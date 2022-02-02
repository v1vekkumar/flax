# Copyright 2022 The Flax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""JAX transformations on Modules.

Jax functional transformations operate on pure functions.
Flax extends these transformations to also operate on Module's which
have stateful variables and PRNG sequences. We refer to these extended
versions as "lifted transformations".

A lifted transformation can be applied to a ``Module`` class or a
function that takes a ``Module`` instance as its first argument.
"""
from typing import Any, Type, Callable, Union, Mapping, Optional, TypeVar, Iterable, Sequence, Tuple

import dataclasses
import functools
import inspect
from flax import errors
from flax import traceback_util
from flax.core import lift, Scope
from flax.linen.module import Module
from flax.linen.module import Variable
from flax.linen.module import wrap_method_once
from flax.linen import module as linen_module
from flax import struct
import jax

traceback_util.register_exclusion(__file__)

# Utils
# -----------------------------------------------------------------------------
def clean_clone(x):
  """Remove scopes and tracers from children."""
  if isinstance(x, Module):
    object.__setattr__(
        x, 'children',
        {k: clean_clone(v) for k, v in x.children.items()})
    object.__setattr__(x, 'scope', None)
  return x


@struct.dataclass
class VariablePlaceholder:
  """Used to mark Variables in a JAX-compatible way when lifting arguments."""
  collection: str = struct.field(pytree_node=False)
  name: str = struct.field(pytree_node=False)


@struct.dataclass
class InstancePlaceholder:
  """Marks module instances in a JAX-compatible way when lifting arguments."""
  cls: type = struct.field(pytree_node=False)
  attrs: dict = struct.field(pytree_node=False)


def get_module_scopes(module, args=None, kwargs=None):
  """Get all scopes on module, including constructor Module arguments.

  To properly functionalize a Module that has other bound Modules passed in
  "from the outside" as dataclass attributes, we need to traverse all dataclass
  fields to find the Scopes associated with the Module.  Additionally, because
  we allow Modules to be passed inside pytrees on the dataclass attributes, we
  must traverse all dataclass attributes as pytrees to find all Modules.  We
  additionally handle lifting Variables (which are just references to data in
  particular scopes) and Module instances that are passed as arguments to
  methods.

  Args:
    module: a bound flax Module.
    args: an *args list possibly containing Variables or Module instances
      referencing a scope.
    kwargs: a **kwargs dict possibly containing Variables or Module instances
      referencing a scope.

  Returns:
    A list of all functional-core Scopes bound on self and inside dataclass
    fields as well as any Scopes passed via argument Variables, an updated args
    list, and an updated kwargs dict that have both had Variables replaced with
    VariablePlaceholders and Module instances replaced with InstancePlaceholders
    that are compatible with jax functions.
  """
  module._try_setup(shallow=True)
  outer_scopes = []
  # gather scopes associated with Variables and Module instances passed as arguments
  def get_arg_scope(x):
    nonlocal outer_scopes
    if isinstance(x, Variable) and isinstance(x.scope, Scope):
      outer_scopes.append(x.scope)
      return VariablePlaceholder(x.collection, x.name)
    elif isinstance(x, Module) and isinstance(x.scope, Scope):
      x._try_setup(shallow=True)
      outer_scopes.append(x.scope)
      attrs = {f.name: getattr(x, f.name)
          for f in dataclasses.fields(x) if f.name != 'parent' and f.init}
      attrs = jax.tree_map(get_arg_scope, attrs)
      return InstancePlaceholder(x.__class__, attrs)
    return x
  new_args, new_kwargs = jax.tree_map(get_arg_scope, (args, kwargs))
  # gather scopes in Variables and Submodules passed as Module attributes
  def get_scope(x):
    nonlocal outer_scopes
    if isinstance(x, Module) and isinstance(x.scope, Scope):
      module_scopes, _, _ = get_module_scopes(x)
      outer_scopes.extend(module_scopes)
    elif isinstance(x, Variable) and isinstance(x.scope, Scope):
      outer_scopes.append(x.scope)
    return x
  attrs = {f.name: getattr(module, f.name)
           for f in dataclasses.fields(module) if f.name != 'parent' and f.init}
  jax.tree_map(get_scope, attrs)
  return outer_scopes + [module.scope,], new_args, new_kwargs


def set_module_scopes(module, args, kwargs, scopes):
  """Set all scopes on module, including those on Modules in dataclass fields.

  To properly functionalize a Module we must also "rehydrate" it with Scopes
  from `get_module_scopes`.  We need to set scopes not just on the Module but
  also on any Module living inside dataclass attributes or even pytrees in its
  dataclass attributes.  We additionally handle restoring Variables and Module
  instances from their placeholders in the method positional and keyword
  arguments.  The order of traversal through this method is the same as in
  `get_module_scopes`, guaranteeing the correct Scopes are applied to each
  Module.

  Args:
    module: a flax Module.
    args: an *args list possibly containing VariablePlaceholder or
      InstancePlaceholder members.
    kwargs: a **kwargs dict possibly containing VariablePlaceholder or
      InstancePlaceholder members.
    scopes: a list of Scopes corresponding to this Module and its arguments that
      was created by the `get_module_scopes` function.

  Returns:
    A copy of the module with it and its attributes bound to the scopes passed
    to this function, an updated args list, and an updated kwargs dict with
    updated Variable and Module instance references.
  """
  idx = 0
  # set scopes associated with Variables and Module instances passed as arguments
  def set_arg_scope(x):
    nonlocal idx
    if isinstance(x, VariablePlaceholder):
      new_x = Variable(scope=scopes[idx], collection=x.collection, name=x.name)
      idx += 1
      return new_x
    elif isinstance(x, InstancePlaceholder):
      instance_scope = scopes[idx]
      idx += 1
      instance_attrs = jax.tree_map(set_arg_scope, x.attrs)
      return x.cls(parent=instance_scope, **instance_attrs)
    return x
  is_placeholder = lambda x: isinstance(x, (VariablePlaceholder, InstancePlaceholder))
  new_args, new_kwargs = jax.tree_map(set_arg_scope,
                                      (args, kwargs),
                                      is_leaf=is_placeholder)
  # set scopes in Variables and Submodules passed as Module attributes
  def set_scopes(module):
    nonlocal idx
    def set_scopes_inner(x):
      nonlocal idx
      if isinstance(x, Module) and isinstance(x.scope, Scope):
        return set_scopes(x)
      elif isinstance(x, Variable) and isinstance(x.scope, Scope):
        new_x = Variable(scope=scopes[idx],
                         collection=x.collection,
                         name=x.name)
        idx += 1
        return new_x
      else:
        return x
    attrs = {f.name: getattr(module, f.name)
             for f in dataclasses.fields(module) if f.name != 'parent' and f.init}
    new_attrs = jax.tree_map(set_scopes_inner, attrs)
    new_module = module.clone(parent=scopes[idx], **new_attrs)
    idx += 1
    return new_module
  new_module = set_scopes(module)
  assert len(scopes) == idx, f'scope list mismatch {len(scopes)} != {idx}'
  return new_module, new_args, new_kwargs


def _test_transformed_return_values(tree, method_name):
  """Tests whether the return value contains any Modules or Variables."""
  impure = any(map(lambda x: isinstance(x, (Module, Variable)),
                   jax.tree_leaves(tree)))
  if impure:
    raise errors.TransformedMethodReturnValueError(method_name)


# Class lifting
# -----------------------------------------------------------------------------
def module_class_lift_transform(
    transform,
    module_class,
    *trafo_args,
    methods=None,
    **trafo_kwargs):
  # TODO(levskaya): find nicer argument convention for multi-method case?

  # Prepare per-method transform args, kwargs.
  if methods is None:
    # Default case, just transform __call__
    class_trafo_args = {'__call__': (trafo_args, trafo_kwargs)}
  elif isinstance(methods, (list, tuple)):
    # Transform every method in methods with given args, kwargs.
    class_trafo_args = {m: (trafo_args, trafo_kwargs) for m in methods}
  elif isinstance(methods, dict):
    # Pass different trafo args per each method.
    assert trafo_args == () and trafo_kwargs == {}, (
        f"""When passing different {transform.__name__} args per method,
        all args must be passed via methods kwarg.""")
    class_trafo_args = {k: ((), v) for k, v in methods.items()}

  # Handle partially initialized module class constructors.
  if (isinstance(module_class, functools.partial) and
      issubclass(module_class.func, Module)):
    partial_object = module_class
    module_class = module_class.func
  else:
    partial_object = None

  def create_trans_fn(fn_name, fn_trafo_args):
    # get existing unbound method from class
    fn = getattr(module_class, fn_name)
    trafo_args, trafo_kwargs = fn_trafo_args
    # we need to create a scope-function from our class for the given method
    @functools.wraps(fn)
    def wrapped_fn(self, *args, **kwargs):
      # make a scope-function to transform
      def core_fn(scopes, *args, **kwargs):
        # make a clone of self using its arguments
        attrs = {f.name: getattr(self, f.name)
                 for f in dataclasses.fields(self) if f.name != 'parent' and f.init}
        # we reference module_class, not self.__class__ to avoid infinite loop
        cloned = module_class(parent=None, **attrs)
        cloned, args, kwargs = set_module_scopes(cloned, args, kwargs, scopes)
        object.__setattr__(cloned, '_state', self._state.export())  # pylint: disable=protected-access
        res = fn(cloned, *args, **kwargs)
        self._state.reimport(cloned._state)  # pylint: disable=protected-access
        _test_transformed_return_values(res, fn_name)
        return res
      # here we apply the given lifting transform to the scope-ingesting fn
      trafo_fn = transform(core_fn, *trafo_args, **trafo_kwargs)
      module_scopes, args, kwargs = get_module_scopes(self, args, kwargs)
      ret = trafo_fn(module_scopes, *args, **kwargs)
      return ret
    return wrapped_fn
  transformed_fns = {fn_name: create_trans_fn(fn_name, fn_trafo_args)
                     for fn_name, fn_trafo_args in class_trafo_args.items()}
  # construct new dynamic class w. transformed methods
  transformed_cls = type(
      transform.__name__.capitalize() + module_class.__name__,
      (module_class,),
      transformed_fns)
  # Handle partially initialized module class constructors.
  if partial_object is not None:
    transformed_cls = functools.partial(transformed_cls,
                                        *partial_object.args,
                                        **partial_object.keywords)
  return transformed_cls


# Function lifting as decorator on methods __inside__ class definition.
# -----------------------------------------------------------------------------
def decorator_lift_transform(transform, class_fn, *trafo_args, 
                             multi_scope=True, **trafo_kwargs):
  # Due to the ordering of method decorators, we must wrap the class_fn
  # with the module state management wrapper first to maintain Module state correctly.
  prewrapped_fn = wrap_method_once(class_fn)
  @functools.wraps(prewrapped_fn)
  def wrapped_fn(self, *args, **kwargs):
    # make a scope-function to transform
    def core_fn(scopes, *args, **kwargs):
      if not multi_scope:
        scopes = [scopes]
      cloned, args, kwargs = set_module_scopes(self, args, kwargs, scopes)
      object.__setattr__(cloned, '_state', self._state.export())  # pylint: disable=protected-access
      res = prewrapped_fn(cloned, *args, **kwargs)
      self._state.reimport(cloned._state)  # pylint: disable=protected-access
      _test_transformed_return_values(res, getattr(class_fn, '__name__', None))
      return res
    # here we apply the given lifting transform to the scope-ingesting fn
    trafo_fn = transform(core_fn, *trafo_args, **trafo_kwargs)
    module_scopes, args, kwargs = get_module_scopes(self, args, kwargs)
    if not multi_scope:
      if len(module_scopes) != 1:
        # TODO transforms like jvp & vjp have args that follow the pytree
        # structure of scopes. The user doesn't explicitly control shared
        # modules passed as arguments to methods or as attributes to Module
        # constructors. Therefore, there is no obvious API for specifying
        # arguments per lifted Module.
        raise NotImplementedError(
            "This transform does not yet support"
            " Modules that include other Modules passed as arguments.")
      module_scopes = module_scopes[0]
    return trafo_fn(module_scopes, *args, **kwargs)
  return wrapped_fn


# Utility to wrap a class or to use as decorator in def of class method.
# -----------------------------------------------------------------------------

TransformTarget = Union[Type[Module], Callable[..., Any]]
Target = TypeVar('Target', bound=TransformTarget)


def _is_module_class(target: TransformTarget) -> bool:
  return (inspect.isclass(target) and issubclass(target, Module)
      or (isinstance(target, functools.partial)) and _is_module_class(target.func))


def lift_transform(transform, target, *trafo_args, methods=None, **trafo_kwargs):
  """Applies to class or as a decorator on class fns."""
  if _is_module_class(target):
    return module_class_lift_transform(
        transform, target, *trafo_args, methods=methods, **trafo_kwargs)
  # we presume this is being used as a function decorator in class definition
  elif callable(target):
    return decorator_lift_transform(
        transform, target, *trafo_args, **trafo_kwargs)
  else:
    raise ValueError(
        'Can only transform a Module subclass or decorate a function'
        ' in class definition.')


def lift_direct_transform(transform, target: Callable[..., Any], mdl: Module,
                          *args, multi_scope=True, **kwargs):
  if _is_module_class(target):
    raise ValueError(
        f'The {transform.__name__} transform can only be applied on a Module method.'
        ' That is function that takes a Module instance as its first arg.')
  elif callable(target):
    aug_transform = lambda fn: functools.partial(transform, fn)
    return decorator_lift_transform(
        aug_transform, target, multi_scope=multi_scope)(mdl, *args, **kwargs)
  else:
    raise ValueError(
        'transform target must be callable')


def vmap(target: Target,
         variable_axes: Mapping[lift.CollectionFilter, lift.InOutAxis],
         split_rngs: Mapping[lift.PRNGSequenceFilter, bool],
         in_axes=0, out_axes=0,
         axis_size: Optional[int] = None,
         axis_name: Optional[str] = None,
         methods=None) -> Target:
  """A lifted version of ``jax.vmap``.

  See ``jax.vmap`` for the unlifted batch transform in Jax.

  ``vmap`` can be used to add a batch axis to a ``Module``.
  For example we could create a version of ``Dense`` with
  a batch axis that does not share parameters::

    BatchDense = nn.vmap(
        nn.Dense,
        in_axes=0, out_axes=0,
        variable_axes={'params': 0},
        split_rngs={'params': True})

  By using ``variable_axes={'params': 0}``, we indicate that the
  parameters themselves are mapped over and therefore not shared along
  the mapped axis. Consequently, we also split the 'params' RNG,
  otherwise the parameters would be initialized identically along
  the mapped axis.

  Similarly, ``vmap`` could be use to add a batch axis with parameter
  sharing::

    BatchFoo = nn.vmap(
        Foo,
        in_axes=0, out_axes=0,
        variable_axes={'params': None},
        split_rngs={'params': False})

  Here we use ``variable_axes={'params': None}`` to indicate the parameter
  variables are shared along the mapped axis. Consequently, the 'params'
  RNG must also be shared.

  Args:
    target: a ``Module`` or a function taking a ``Module``
      as its first argument.
    variable_axes: the variable collections that are lifted into the
      batching transformation. Use `None` to indicate a broadcasted
      collection or an integer to map over an axis.
    split_rngs: Split PRNG sequences will be different for each index
      of the batch dimension. Unsplit PRNGs will be broadcasted.
    in_axes: Specifies the mapping of the input arguments (see `jax.vmap).
    out_axes: Specifies the mapping of the return value (see `jax.vmap).
    axis_size: Specifies the size of the batch axis. This only needs
      to be specified if it cannot be derived from the input arguments.
    axis_name: Specifies a name for the batch axis. Can be used together
      with parallel reduction primitives (e.g. `jax.lax.pmean`,
      `jax.lax.ppermute`, etc.)
  """
  return lift_transform(
      lift.vmap, target, variable_axes, split_rngs,
      methods=methods,
      in_axes=in_axes, out_axes=out_axes,
      axis_size=axis_size, axis_name=axis_name)


def jit(target: Target,
        variables: lift.CollectionFilter = True,
        rngs: lift.PRNGSequenceFilter = True,
        static_argnums: Union[int, Iterable[int]] = (),
        donate_argnums: Union[int, Iterable[int]] = (),
        device=None,
        backend: Union[str, None] = None,
        methods=None) -> Target:
  """Lifted version of ``jax.jit``.

  Args:
    target: a ``Module`` or a function taking a ``Module``
      as its first argument.
    variables: The variable collections that are lifted. By default all
      collections are lifted.
    rngs: The PRNG sequences that are lifted. By default all PRNG sequences
      are lifted.
    static_argnums: An int or collection of ints specifying which positional
      arguments to treat as static (compile-time constant). Operations that only
      depend on static arguments will be constant-folded in Python (during
      tracing), and so the corresponding argument values can be any Python
      object. Static arguments should be hashable, meaning both ``__hash__`` and
      ``__eq__`` are implemented, and immutable. Calling the jitted function
      with different values for these constants will trigger recompilation. If
      the jitted function is called with fewer positional arguments than
      indicated by ``static_argnums`` then an error is raised. Arguments that
      are not arrays or containers thereof must be marked as static.
      Defaults to ().
    device: This is an experimental feature and the API is likely to change.
      Optional, the Device the jitted function will run on. (Available devices
      can be retrieved via :py:func:`jax.devices`.) The default is inherited from
      XLA's DeviceAssignment logic and is usually to use ``jax.devices()[0]``.
    backend: a string representing the XLA backend: ``'cpu'``, ``'gpu'``, or
      ``'tpu'``.
    donate_argnums: Specify which arguments are "donated" to the computation.
      It is safe to donate arguments if you no longer need them once the
      computation has finished. In some cases XLA can make use of donated
      buffers to reduce the amount of memory needed to perform a computation,
      for example recycling one of your input buffers to store a result. You
      should not reuse buffers that you donate to a computation, JAX will raise
      an error if you try to.

  Returns:
    A wrapped version of target, set up for just-in-time compilation.
  """
  return lift_transform(
      lift.jit, target,
      variables=variables, rngs=rngs,
      static_argnums=static_argnums,
      donate_argnums=donate_argnums,
      device=device,
      backend=backend,
      methods=methods)


def checkpoint(target: Target,
        variables: lift.CollectionFilter = True,
        rngs: lift.PRNGSequenceFilter = True,
        concrete: bool = False,
        prevent_cse: bool = True,
        policy: Optional[Callable[..., bool]] = None,
        methods=None) -> Target:
  """Lifted version of ``jax.checkpoint``.

  This function is aliased to ``lift.remat`` just like ``jax.remat``.

  Args:
    target: a ``Module`` or a function taking a ``Module``
      as its first argument. intermediate computations will be
      re-computed when computing gradients for the target.
    variables: The variable collections that are lifted. By default all
      collections are lifted.
    rngs: The PRNG sequences that are lifted. By default all PRNG sequences
      are lifted.
    concrete: Optional, boolean indicating whether ``fun`` may involve
      value-dependent Python control flow (default False). Support for such
      control flow is optional, and disabled by default, because in some
      edge-case compositions with :func:`jax.jit` it can lead to some extra
      computation.
    prevent_cse: Optional, boolean indicating whether to prevent common
      subexpression elimination (CSE) optimizations in the HLO generated from
      differentiation. This CSE prevention has costs because it can foil other
      optimizations, and because it can incur high overheads on some backends,
      especially GPU. The default is True because otherwise, under a ``jit`` or
      ``pmap``, CSE can defeat the purpose of this decorator. But in some
      settings, like when used inside a ``scan``, this CSE prevention mechanism
      is unnecessary, in which case ``prevent_cse`` should be set to False.
    policy: Experimental checkpoint policy, see ``jax.checkpoint``.
  Returns:
    A wrapped version of ``target``. When computing gradients intermediate
    computations will be re-computed on the backward pass.
  """
  return lift_transform(
      lift.checkpoint, target,
      variables=variables, rngs=rngs, concrete=concrete,
      prevent_cse=prevent_cse, policy=policy,
      methods=methods)


remat = checkpoint


def remat_scan(target: Target,
               lengths: Sequence[int],
               policy: Optional[Callable[..., bool]] = None,
               variable_broadcast: lift.CollectionFilter = False,
               variable_carry: lift.CollectionFilter = False,
               variable_axes: Mapping[lift.CollectionFilter, lift.InOutScanAxis] = {True: 0},
               split_rngs: Mapping[lift.PRNGSequenceFilter, bool] = {True: True}) -> Target:
  """Combines remat and scan for memory efficiency and constant time compilation.

  ``remat_scan`` allows for constant compile times and sublinear
  memory usage with respect to model depth. At a small constant
  penalty. This is typically beneficial for very deep models.

  Example::

    class BigModel(nn.Module):
      @nn.compact
      def __call__(self, x):
        DenseStack = nn.remat_scan(nn.Dense, lengths=(10, 10))
        # 100x dense with O(sqrt(N)) memory for gradient computation
        return DenseStack(8, name="dense_stack")(x)

  Args:
    target: a ``Module`` or a function taking a ``Module``
      as its first argument.
    lengths: number of loop iterations at the given level. The total
      number of iterations `n = prod(lengths)`. each loop is rematerialized.
      This way the memory consumption is proportional to `n^(1 / d)` where `d = len(lengths)`.
      Minimal memory consumptions requires tuning the lengths such that the same amount of memory
      is consumed at each level of the nested loop.
    variable_broadcast: Specifies the broadcasted variable collections.
      A broadcasted variable should not depend on any computation that cannot be lifted out of the loop.
      This is typically used to define shared parameters inside the fn.
    variable_carry: Specifies the variable collections that are carried through the loop.
      Mutations to these variables are carried to the next iteration and will be preserved
      when the scan finishes.
    variable_axes: the variable collections that are scanned over.
    split_rngs: Split PRNG sequences will be different for each loop iterations.
      If split is False the PRNGs will be the same across iterations.
  Returns:
    A wrapped version of ``target`` that repeats itself prod(lengths) times.
  """
  return lift_transform(
      lift.remat_scan, target,
      lengths=lengths,
      variable_broadcast=variable_broadcast,
      variable_carry=variable_carry,
      variable_axes=variable_axes,
      split_rngs=split_rngs,
      policy=policy,
  )


def scan(target: Target,
         variable_axes: Mapping[lift.CollectionFilter, lift.InOutScanAxis] = {},
         variable_broadcast: lift.CollectionFilter = False,
         variable_carry: lift.CollectionFilter = False,
         split_rngs: Mapping[lift.PRNGSequenceFilter, bool] = {},
         in_axes=0, out_axes=0,
         length: Optional[int] = None,
         reverse: bool = False,
         data_transform: Optional[Callable[..., Any]] = None,
         methods=None) -> Target:
  """A lifted version of ``jax.lax.scan``.

  See ``jax.lax.scan`` for the unlifted scan in Jax.

  To improve consistency with ``vmap``, this version of scan
  uses ``in_axes`` and ``out_axes`` to determine which arguments
  are scanned over and along which axis.

  ``scan`` distinguishes between 3 different types of values inside the loop:

  #. **scan**: a value that is iterated over in a loop. All scan values must
     have the same size in the axis they are scanned over. Scanned outputs
     will be stacked along the scan axis.

  #. **carry**: A carried value is updated at each loop iteration. It must
     have the same shape and dtype throughout the loop.

  #. **broadcast**: a value that is closed over by the loop. When a variable
     is broadcasted they are typically initialized inside the loop body but
     independent of the loop variables.

  The loop body should have the signature
  ``(scope, body, carry, *xs) -> (carry, ys)``, where ``xs`` and ``ys``
  are the scan values that go in and out of the loop.

  Example::

    import flax
    import flax.linen as nn
    from jax import random

    class SimpleScan(nn.Module):
      @nn.compact
      def __call__(self, c, xs):
        LSTM = nn.scan(nn.LSTMCell,
                       variable_broadcast="params",
                       split_rngs={"params": False},
                       in_axes=1,
                       out_axes=1)
        return LSTM()(c, xs)

    seq_len, batch_size, in_feat, out_feat = 20, 16, 3, 5
    key_1, key_2, key_3 = random.split(random.PRNGKey(0), 3)

    xs = random.uniform(key_1, (batch_size, seq_len, in_feat))
    init_carry = nn.LSTMCell.initialize_carry(key_2, (batch_size,), out_feat)

    model = SimpleScan()
    variables = model.init(key_3, init_carry, xs)
    out_carry, out_val = model.apply(variables, init_carry, xs)

    assert out_val.shape == (batch_size, seq_len, out_feat)

  Args:
    target: a ``Module`` or a function taking a ``Module``
      as its first argument.
    variable_axes: the variable collections that are scanned over.
    variable_broadcast: Specifies the broadcasted variable collections.
      A broadcasted variable should not depend on any computation that cannot be lifted out of the loop.
      This is typically used to define shared parameters inside the fn.
    variable_carry: Specifies the variable collections that are carried through the loop.
      Mutations to these variables are carried to the next iteration and will be preserved
      when the scan finishes.
    split_rngs: Split PRNG sequences will be different for each loop iterations.
      If split is False the PRNGs will be the same across iterations.
    in_axes: Specifies the axis to scan over for the arguments. Should be a prefix
      tree of the arguments. Use `flax.core.broadcast` to feed an entire input
      to each iteration of the scan body.
    out_axes: Specifies the axis to scan over for the return value. Should be a prefix
      tree of the return value.
    length: Specifies the number of loop iterations. This only needs
      to be specified if it cannot be derivied from the scan arguments.
    reverse: If true, scan from end to start in reverse order.
    data_transform: optional function to transform raw functional-core variable
      and rng groups inside lifted scan body_fn, intended for inline SPMD
      annotations.

  Returns:
    The scan function with the signature ``(scope, carry, *xxs) -> (carry, yys)``,
    where ``xxs`` and ``yys`` are the scan values that go in and out of the loop.
  """
  return lift_transform(
      lift.scan, target,
      variable_axes=variable_axes,
      variable_broadcast=variable_broadcast,
      variable_carry=variable_carry,
      split_rngs=split_rngs,
      in_axes=in_axes, out_axes=out_axes,
      length=length,
      reverse=reverse,
      data_transform=data_transform,
      methods=methods)


def map_variables(
    target: Target,
    mapped_collections: lift.CollectionFilter,
    trans_in_fn: Callable[..., Any] = lift.id_fn,
    trans_out_fn: Callable[..., Any] = lift.id_fn,
    init: bool = False, mutable: bool = False,
    rngs: lift.PRNGSequenceFilter = True,
    variables: lift.CollectionFilter = True) -> Target:
  """Map Variables inside a module.

  Example::

    class OneBitDense(nn.Module):
      @nn.compact
      def __call__(self, x):
        def sign(x):
          return jax.tree_map(jnp.sign, x)
        MapDense = nn.map_variables(nn.Dense, "params", sign, init=True)
        return MapDense(4)(x)

  Args:
    fn: the function to be transformed.
    mapped_collections: the collection(s) to be transformed.
    map_in_fn: creates a view of the target variables.
    map_out_fn: transforms the updated variables in the view after mutation.
    init: If True, variables are initialized before transformation.
    mutable: If True, the mapped variable collections will be mutable.
    rngs: PRNGSequences added to the transformed scope (default: all).
    variables: Additional Variable collections added to the transformed scope.
      Besides those specified by `target` (default: all).
  Returns:
    a wrapped version of ``target`` that will map the specificied collections.
  """

  return lift_transform(
      lift.map_variables, target,
      mapped_collections,
      trans_in_fn, trans_out_fn,
      init, mutable,
      rngs, variables
  )


def vjp(fn: Callable[..., Any], mdl: Module, *primals,
    has_aux: bool = False, reduce_axes=(),
    vjp_variables: lift.CollectionFilter = "params",
    variables: lift.CollectionFilter = True,
    rngs: lift.PRNGSequenceFilter = True, 
    ) -> Tuple[Any, Any]:
  """A lifted version of ``jax.vjp``.

  See ``jax.vjp`` for the unlifted vector-Jacobiam product (backward gradient).

  Note that a gradient is returned for all variables in the collections
  specified by `vjp_variables`. However, the backward funtion only expects
  a cotangent for the return value of `fn`. If variables require a co-tangent
  as well they can be returned from `fn` using `Module.variables`.

  Example::

    class LearnScale(nn.Module):
      @nn.compact
      def __call__(self, x):
        p = self.param('scale', nn.initializers.zeros, ())
        return p * x

    class Foo(nn.Module):
      @nn.compact
      def __call__(self, x):
        y, bwd = nn.vjp(lambda mdl, x: mdl(x), LearnScale(), x)
        params_grad, x_grad = bwd(jnp.ones(y.shape))
        return y, params_grad, x_grad

  Args:
    fn: Function to be differentiated. Its arguments should be arrays, scalars,
      or standard Python containers of arrays or scalars. It should return an
      array, scalar, or standard Python container of arrays or scalars. It will
      receive the scope and primals as arguments.
    scope: The scope of which the variables will be differentiated.
    primals: A sequence of primal values at which the Jacobian of ``fn``
      should be evaluated. The length of ``primals`` should be equal to the
      number of positional parameters to ``fn``. Each primal value should be a
      tuple of arrays, scalar, or standard Python containers thereof.
    has_aux: Optional, bool. Indicates whether ``fn`` returns a pair where the
     first element is considered the output of the mathematical function to be
     differentiated and the second element is auxiliary data. Default False.
    reduce_axes: Optional, tuple of axis names. If an axis is listed here, and
      ``fn`` implicitly broadcasts a value over that axis, the backward pass
      will perform a ``psum`` of the corresponding gradient. Otherwise, the
      VJP will be per-example over named axes. For example, if ``'batch'``
      is a named batch axis, ``vjp(f, *args, reduce_axes=('batch',))`` will
      create a VJP function that sums over the batch while ``vjp(f, *args)``
      will create a per-example VJP.
    vjp_variables: The vjpfun will return a cotangent vector for all
      variable collections specified by this filter.
    variables: other variables collections that are available inside `fn` but
      do not receive a cotangent.
    rngs: the prngs that are available inside `fn`.

  Returns:
    If ``has_aux`` is ``False``, returns a ``(primals_out, vjpfun)`` pair, where
    ``primals_out`` is ``fn(*primals)``.
    ``vjpfun`` is a function from a cotangent vector with the same shape as
    ``primals_out`` to a tuple of cotangent vectors with the same shape as
    ``primals``, representing the vector-Jacobian product of ``fn`` evaluated at
    ``primals``. If ``has_aux`` is ``True``, returns a
    ``(primals_out, vjpfun, aux)`` tuple where ``aux`` is the auxiliary data
    returned by ``fn``.
  """
  return lift_direct_transform(
      lift.vjp, fn, mdl, *primals,
      multi_scope=False,
      has_aux=has_aux, reduce_axes=reduce_axes,
      vjp_variables=vjp_variables,
      variables=variables,
      rngs=rngs)


def jvp(fn: Callable[..., Any], mdl: Module,
    primals, tangents, variable_tangents,
    variables: lift.CollectionFilter = True,
    rngs: lift.PRNGSequenceFilter = True, 
    ) -> Union[Tuple[Any, Callable], Tuple[Any, Callable, Any]]:
  """A lifted version of ``jax.jvp``.

  See ``jax.jvp`` for the unlifted Jacobian-vector product (forward gradient).

  Note that no tangents are returned for variables. When variable tangents
  are required their value should be returned explicitly by `fn`
  using `Module.variables`::

    class LearnScale(nn.Module):
      @nn.compact
      def __call__(self, x):
        p = self.param('test', nn.initializers.zeros, ())
        return p * x

    class Foo(nn.Module):
      @nn.compact
      def __call__(self, x):
        scale = LearnScale()
        vars_t = jax.tree_map(jnp.ones_like, scale.variables.get('params', {}))
        _, out_t = nn.jvp(
            lambda mdl, x: mdl(x), scale, (x,), (jnp.zeros_like(x),),
            variable_tangents={'params': vars_t})
        return out_t

  Example::

    def learn_scale(scope, x):
      p = scope.param('scale', nn.initializers.zeros, ())
      return p * x

    def f(scope, x):
      vars_t = jax.tree_map(jnp.ones_like, scope.variables().get('params', {}))
      x, out_t = lift.jvp(
          learn_scale, scope, (x,), (jnp.zeros_like(x),),
          variable_tangents={'params': vars_t})
      return out_t

  Args:
    primals: The primal values at which the Jacobian of ``fun`` should be
      evaluated. Should be either a tuple or a list of arguments,
      and its length should be equal to the number of positional parameters of
      ``fun``.
    tangents: The tangent vector for which the Jacobian-vector product should be
      evaluated. Should be either a tuple or a list of tangents, with the same
      tree structure and array shapes as ``primals``.
    variable_tangents: A dict or PyTree fo dicts with the same structure as
      scopes. Each entry in the dict specifies the tangents for a variable
      collection. Not specifying a collection in variable_tangents is
      equivalent to passing a zero vector as the tangent.
    variables: other variables collections that are available in `fn` but
      do not receive a tangent.
    rngs: the prngs that are available inside `fn`.

  Returns:
    A ``(primals_out, tangents_out)`` pair, where ``primals_out`` is
    ``fun(*primals)``, and ``tangents_out`` is the Jacobian-vector product of
    ``function`` evaluated at ``primals`` with ``tangents``. The
    ``tangents_out`` value has the same Python tree structure and shapes as
    ``primals_out``.
  """
  return lift_direct_transform(
      lift.jvp, fn, mdl, primals, tangents, variable_tangents,
      multi_scope=False,
      variables=variables,
      rngs=rngs)


# a version of lift.custom_vjp with a single scope function
# this avoids having to lift multiple functions in
# lift_transform.
def _custom_vjp_single_scope_fn(
    fn: Callable[..., Any],
    backward_fn: Callable[..., Any],
    grad_vars: lift.CollectionFilter = 'params',
    nondiff_argnums=()):
  nodiff_fn = functools.partial(fn, needs_residual=False)
  forward_fn = functools.partial(fn, needs_residual=True)
  return lift.custom_vjp(
    nodiff_fn, forward_fn, backward_fn,
    grad_vars, nondiff_argnums)


def custom_vjp(fn: Callable[..., Any],
    forward_fn: Callable[..., Any],
    backward_fn: Callable[..., Any],
    grad_vars: lift.CollectionFilter = 'params',
    nondiff_argnums=()):
  """Lifted version of `jax.custom_vjp`.

  `forward_fn` and `backward_fn` together define a custom vjp for `fn`.
  The original `fn` will run in case a vjp (backward gradient) is not computed.

  The `forward_fn` receives the same arguments as `fn` but is expected to return
  a tuple containing the output of `fn(mdl, *args)` and the residuals that are
  passed to `backward_fn`.

  The `backward_fn` receives the nondiff arguments, residuals, and the output
  tangents. It should return a tuple containing the input and variable tangents.

  Note that the vjp function returned by `nn.vjp` can be passed as residual and
  used in the `backward_fn`. The scope is unavailable during the backward pass.
  If the module is required in `backward_fn`, a snapshot of the variables can
  be taken and returned as a residual in the `forward_fn`.

  Example::

    class Foo(nn.Module):
      @nn.compact
      def __call__(self, x):
        def f(mdl, x):
          return mdl(x)

        def fwd(mdl, x):
          return nn.vjp(f, mdl, x)

        def bwd(vjp_fn, y_t):
          input_t, params_t = vjp_fn(y_t)
          params_t = jax.tree_map(jnp.sign, params_t)
          return input_t, params_t

        sign_grad = nn.custom_vjp(
            f, forward_fn=fwd, backward_fn=bwd)
        return sign_grad(nn.Dense(1), x).reshape(())

    x = jnp.ones((2,))
    variables = Foo().init(random.PRNGKey(0), x)
    grad = jax.grad(Foo().apply)(variables, x)

  Args:
    fn: The function to define a custom_vjp for.
    forward_fn: A function with the same arguments as `fn` returning an tuple
      with the original output and the residuals that will be passsed to
      `backward_fn`.
    backward_fn: arguments are passed as (\*nondiff_args, residuals, tangents)
      The function should return a tuple containing the tangents for the
      input arguments (except the module and nondiff args) and the variable
      tangents for the collections specified by `grad_vars`.
    grad_vars: The collections for which a vjp will be computed
      (default: "params").
    nondiff_argnums: arguments for which no vjp is computed.
  Returns:
    A function with the same signature as `fn` with the custom vjp.
  """
  def shared_forward_fn(*args, needs_residual, **kwargs):
    if needs_residual:
      return forward_fn(*args, **kwargs)
    else:
      return fn(*args, ** kwargs)
  return decorator_lift_transform(
      _custom_vjp_single_scope_fn, shared_forward_fn,
      backward_fn=backward_fn, grad_vars=grad_vars,
      nondiff_argnums=nondiff_argnums,
      multi_scope=False)


# Special case of decorator_lift_transform to handle named calls for profiling.
def named_call(class_fn, force=True):
  """Labels a method for labelled traces in profiles.

  Args:
    force: If True, the named_call transform is applied even if it is globally disabled.
      (e.g.: by calling `flax.linen.disable_named_call()`)
  """
  # Due to the ordering of method decorators, we must wrap the class_fn
  # with the module state management wrapper first to maintain Module state correctly.
  prewrapped_fn = wrap_method_once(class_fn)
  @functools.wraps(prewrapped_fn)
  def wrapped_fn(self, *args, **kwargs):
    if not force and not linen_module._use_named_call:
      return prewrapped_fn(self, *args, **kwargs)
    fn_name = class_fn.__name__
    method_suffix = f'.{fn_name}' if fn_name != '__call__' else ''
    module_name = self.name or self.__class__.__name__
    full_name = f'{module_name}{method_suffix}'
    # make a scope-function to transform
    def core_fn(scopes, *args, **kwargs):
      cloned, args, kwargs = set_module_scopes(self, args, kwargs, scopes)
      object.__setattr__(cloned, '_state', self._state.export())  # pylint: disable=protected-access
      res = prewrapped_fn(cloned, *args, **kwargs)
      self._state.reimport(cloned._state)  # pylint: disable=protected-access
      _test_transformed_return_values(res, fn_name)
      return res
    # here we apply the given lifting transform to the scope-ingesting fn
    trafo_fn = lift.named_call(core_fn, full_name)
    module_scopes, args, kwargs = get_module_scopes(self, args, kwargs)
    return trafo_fn(module_scopes, *args, **kwargs)
  return wrapped_fn
