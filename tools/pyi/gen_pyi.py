
import os
import collections
from pprint import pformat

import argparse

from tools.codegen.model import *
from tools.codegen.api.python import *
from typing import Sequence, List, Mapping, Dict

from ..autograd.utils import CodeTemplate, write
from ..autograd.gen_python_functions import should_generate_py_binding, load_signatures, group_overloads

"""
This module implements generation of type stubs for PyTorch,
enabling use of autocomplete in IDEs like PyCharm, which otherwise
don't understand C extension modules.

At the moment, this module only handles type stubs for torch and
torch.Tensor.  It should eventually be expanded to cover all functions
which come are autogenerated.

Here's our general strategy:

- We start off with a hand-written __init__.pyi.in file.  This
  file contains type definitions for everything we cannot automatically
  generate, including pure Python definitions directly in __init__.py
  (the latter case should be pretty rare).

- We go through automatically bound functions based on the
  type information recorded in native_functions.yaml and
  generate type hints for them (generate_type_hints)

There are a number of type hints which we've special-cased;
read gen_pyi for the gory details.
"""

# TODO: consider waiting to group by base name until we actually need to
# (after computing type hint signatures, when adding @overload directives)
def group_by_base_name(python_funcs: Sequence[PythonSignatureNativeFunctionPair]) -> Mapping[str, List[PythonSignatureGroup]]:
    groups = group_overloads(python_funcs, sort=False)
    d = collections.defaultdict(list)
    for g in groups:
        name = g.signature.name
        d[name].append(g)
    return d

def get_py_torch_functions(
        python_funcs: Sequence[PythonSignatureNativeFunctionPair],
        method: bool = False,
) -> Mapping[str, Sequence[PythonSignatureGroup]]:
    """
    Get declarations (grouped by name) which should be generated
    as either functions in the "torch" module or methods on Tensor.
    """
    def should_bind_function(python_func: PythonSignatureNativeFunctionPair) -> bool:
        return (should_generate_py_binding(python_func.function) and
                not python_func.function.python_module and
                Variant.function in python_func.function.variants)

    def should_bind_method(python_func: PythonSignatureNativeFunctionPair) -> bool:
        return (should_generate_py_binding(python_func.function) and
                not python_func.function.python_module and
                Variant.method in python_func.function.variants)

    should_bind = should_bind_method if method else should_bind_function
    return group_by_base_name([f for f in python_funcs if should_bind(f)])


# TODO: Consider defining some aliases for our Union[...] types, to make
# the stubs to read on the human eye.

DEVICE_PARAM = "device: Union[_device, str, None]=None"
FACTORY_PARAMS = f"dtype: Optional[_dtype]=None, {DEVICE_PARAM}, requires_grad: _bool=False"

# this could be more precise w.r.t list contents etc. How to do Ellipsis?
INDICES = "indices: Union[None, _int, slice, Tensor, List, Tuple]"

blocklist = [
    '__init_subclass__',
    '__new__',
    '__subclasshook__',
    'cdist',
    'clamp',
    'clamp_',
    'device',
    'grad',
    'requires_grad',
    'range',
    # defined in functional
    'einsum',
    # reduction argument; these bindings don't make sense
    'binary_cross_entropy_with_logits',
    'ctc_loss',
    'cosine_embedding_loss',
    'hinge_embedding_loss',
    'kl_div',
    'margin_ranking_loss',
    'triplet_margin_loss',
    # Somehow, these are defined in both _C and in functional. Ick!
    'broadcast_tensors',
    # Manually define named tensor type stubs in __init__.pyi.in
    'align_tensors',
    'meshgrid',
    'cartesian_prod',
    'block_diag',
    'norm',
    'chain_matmul',
    'stft',
    'istft',
    'tensordot',
    'split',
    'unique_consecutive',
    'atleast_1d',
    'atleast_2d',
    'atleast_3d',
    # These are handled specially by python_arg_parser.cpp
    'add',
    'add_',
    'add_out',
    'sub',
    'sub_',
    'sub_out',
    'mul',
    'mul_',
    'mul_out',
    'div',
    'div_',
    'div_out',
    'true_divide', 'true_divide_', 'true_divide_out',
    'floor_divide', 'floor_divide_', 'floor_divide_out',
]


binary_ops = ('add', 'sub', 'mul', 'div', 'pow', 'lshift', 'rshift', 'mod', 'truediv',
              'matmul', 'floordiv',
              'radd', 'rsub', 'rmul', 'rtruediv', 'rfloordiv', 'rpow',          # reverse arithmetic
              'and', 'or', 'xor',                   # logic
              'iadd', 'iand', 'idiv', 'ilshift', 'imul',
              'ior', 'irshift', 'isub', 'ixor',  # inplace ops
              )
comparison_ops = ('eq', 'ne', 'ge', 'gt', 'lt', 'le')
unary_ops = ('neg', 'abs', 'invert')
to_py_type_ops = ('bool', 'float', 'complex', 'long', 'index', 'int', 'nonzero')
all_ops = binary_ops + comparison_ops + unary_ops + to_py_type_ops


def sig_for_ops(opname: str) -> List[str]:
    """sig_for_ops(opname : str) -> List[str]

    Returns signatures for operator special functions (__add__ etc.)"""

    # we have to do this by hand, because they are hand-bound in Python

    assert opname.endswith('__') and opname.startswith('__'), "Unexpected op {}".format(opname)

    name = opname[2:-2]
    if name in binary_ops:
        return ['def {}(self, other: Any) -> Tensor: ...'.format(opname)]
    elif name in comparison_ops:
        # unsafe override https://github.com/python/mypy/issues/5704
        return ['def {}(self, other: Any) -> Tensor: ...  # type: ignore'.format(opname)]
    elif name in unary_ops:
        return ['def {}(self) -> Tensor: ...'.format(opname)]
    elif name in to_py_type_ops:
        if name in {'bool', 'float', 'complex'}:
            tname = name
        elif name == 'nonzero':
            tname = 'bool'
        else:
            tname = 'int'
        if tname in {'float', 'int', 'bool', 'complex'}:
            tname = 'builtins.' + tname
        return ['def {}(self) -> {}: ...'.format(opname, tname)]
    else:
        raise Exception("unknown op", opname)

def generate_named_tuples(funcs: Sequence[PythonSignatureGroup]) -> Dict[str, str]:
    namedtuples: Dict[str, str] = {}
    for sig_group in funcs:
        named_tuple = sig_group.signature.returns.named_tuple_pyi()
        if named_tuple is not None:
            tuple_name, tuple_def = named_tuple
            if tuple_name in namedtuples:
                assert namedtuples[tuple_name] == tuple_def
            else:
                namedtuples[tuple_name] = tuple_def
    return namedtuples

def generate_type_hints(funcs: Sequence[PythonSignatureGroup], is_tensor: bool = False) -> List[str]:
    """generate_type_hints(funcs, is_tensor=False)

    Generates type hints for the declarations pertaining to the function
    :attr:`funcs` are the func from the parsed native_functions.yaml.
    The :attr:`is_tensor` flag indicates whether we are parsing
    members of the Tensor class (true) or functions in the
    `torch` namespace (default, false).
    """

    type_hints = []
    any_out = any([g for g in funcs if g.outplace is not None])

    for sig_group in funcs:
        # Some deprecated ops that are on the blocklist are still included in pyi
        if sig_group.signature.name in blocklist and not sig_group.signature.deprecated:
            continue

        # deprecated signatures have separate entries for their functional and out variants
        # (as opposed to the native ops, which fuse the two into a single signature).
        # generate the functional variant here, if an out variant exists.
        if sig_group.signature.deprecated and sig_group.outplace is not None:
            type_hint = sig_group.signature.signature_str_pyi(skip_outputs=True)
            type_hints.append(type_hint)

        # TODO: remove HACK
        # the pyi codegen currently adds an optional out param in cases where the current op does NOT have an out variant,
        # but an overload of the op DOES have an out variant.
        # TODO: After that, we should consider killing this method entirely and operating per PythonSignatureGroup
        # rather than grouping their overloads together
        # (since there isn't much else semantically meaningful about grouping overloads)
        # this hack also doesn't apply to deprecated ops
        hacky_add_output = any_out and sig_group.outplace is None and not sig_group.signature.deprecated
        # PythonSignatureGroups that have both a functional + out variant get a single signature, with an optional out argument
        # Generates the out variant if one exists. Otherwise, generate the functional variant
        type_hint = sig_group.signature.signature_str_pyi(
            skip_outputs=sig_group.outplace is None, hacky_add_output=hacky_add_output)
        type_hints.append(type_hint)

        # Some operators also additionally have a vararg variant of their signature
        type_hint_vararg = sig_group.signature.signature_str_pyi_vararg(
            skip_outputs=sig_group.outplace is None, hacky_add_output=hacky_add_output)
        if type_hint_vararg:
            type_hints.append(type_hint_vararg)

    return type_hints

def gen_nn_functional(out: str) -> None:
    # Functions imported into `torch.nn.functional` from `torch`, perhaps being filtered
    # through an `_add_docstr` call
    imports = [
        'conv1d',
        'conv2d',
        'conv3d',
        'conv_transpose1d',
        'conv_transpose2d',
        'conv_transpose3d',
        'conv_tbc',
        'avg_pool1d',
        'relu_',
        'selu_',
        'celu_',
        'rrelu_',
        'pixel_shuffle',
        'channel_shuffle',
        'pdist',
        'cosine_similarity',
    ]
    # Functions generated by `torch._jit_internal.boolean_dispatch`
    dispatches = [
        'fractional_max_pool2d',
        'fractional_max_pool3d',
        'max_pool1d',
        'max_pool2d',
        'max_pool3d',
        'adaptive_max_pool1d',
        'adaptive_max_pool2d',
        'adaptive_max_pool3d',
    ]
    # Functions directly imported from `torch._C`
    from_c = [
        'avg_pool2d',
        'avg_pool3d',
        'hardtanh_',
        'elu_',
        'leaky_relu_',
        'logsigmoid',
        'softplus',
        'softshrink',
        'one_hot',
    ]
    import_code = ["from .. import {0} as {0}".format(_) for _ in imports]
    # TODO make these types more precise
    dispatch_code = ["{}: Callable".format(_) for _ in (dispatches + from_c)]
    stubs = CodeTemplate.from_file(os.path.join('torch', 'nn', 'functional.pyi.in'))
    env = {
        'imported_hints': import_code,
        'dispatched_hints': dispatch_code
    }
    write(out, 'torch/nn/functional.pyi', stubs, env)

    # functional.pyi already contains the definitions for those functions
    # so, we don't export then to it
    from_c.extend(['hardtanh', 'leaky_relu', 'hardsigmoid'])
    dispatch_code = ["{}: Callable".format(_) for _ in (dispatches + from_c)]
    env = {
        'imported_hints': import_code,
        'dispatched_hints': dispatch_code
    }
    stubs = CodeTemplate.from_file(os.path.join('torch', '_C', '_nn.pyi.in'))
    write(out, 'torch/_C/_nn.pyi', stubs, env)

def gen_nn_pyi(out: str) -> None:
    gen_nn_functional(out)

def gen_pyi(native_yaml_path: str, deprecated_yaml_path: str, out: str) -> None:
    """gen_pyi()

    This function generates a pyi file for torch.
    """

    # Some of this logic overlaps with generate_python_signature in
    # tools/autograd/gen_python_functions.py; however, this
    # function is all about generating mypy type signatures, whereas
    # the other function generates are custom format for argument
    # checking.  If you are update this, consider if your change
    # also needs to update the other file.

    # Dictionary for NamedTuple definitions
    namedtuples: Dict[str, str] = {}

    # Generate type signatures for top-level functions
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    unsorted_function_hints: Dict[str, List[str]] = collections.defaultdict(list)
    unsorted_function_hints.update({
        'set_flush_denormal': ['def set_flush_denormal(mode: _bool) -> _bool: ...'],
        'get_default_dtype': ['def get_default_dtype() -> _dtype: ...'],
        'from_numpy': ['def from_numpy(ndarray) -> Tensor: ...'],
        'numel': ['def numel(self: Tensor) -> _int: ...'],
        'clamp': ["def clamp(self, min: _float=-inf, max: _float=inf,"
                  " *, out: Optional[Tensor]=None) -> Tensor: ..."],
        'as_tensor': ["def as_tensor(data: Any, dtype: _dtype=None, device: Optional[_device]=None) -> Tensor: ..."],
        'get_num_threads': ['def get_num_threads() -> _int: ...'],
        'set_num_threads': ['def set_num_threads(num: _int) -> None: ...'],
        'init_num_threads': ['def init_num_threads() -> None: ...'],
        'get_num_interop_threads': ['def get_num_interop_threads() -> _int: ...'],
        'set_num_interop_threads': ['def set_num_interop_threads(num: _int) -> None: ...'],
        # These functions are explicitly disabled by
        # SKIP_PYTHON_BINDINGS because they are hand bound.
        # Correspondingly, we must hand-write their signatures.
        'tensor': ["def tensor(data: Any, {}) -> Tensor: ...".format(FACTORY_PARAMS)],
        'sparse_coo_tensor': ['def sparse_coo_tensor(indices: Tensor, values: Union[Tensor,List],'
                              ' size: Optional[_size]=None, *, dtype: Optional[_dtype]=None,'
                              ' device: Union[_device, str, None]=None, requires_grad:_bool=False) -> Tensor: ...'],
        'range': ['def range(start: Number, end: Number,'
                  ' step: Number=1, *, out: Optional[Tensor]=None, {}) -> Tensor: ...'
                  .format(FACTORY_PARAMS)],
        'arange': ['def arange(start: Number, end: Number, step: Number, *,'
                   ' out: Optional[Tensor]=None, {}) -> Tensor: ...'
                   .format(FACTORY_PARAMS),
                   'def arange(start: Number, end: Number, *, out: Optional[Tensor]=None, {}) -> Tensor: ...'
                   .format(FACTORY_PARAMS),
                   'def arange(end: Number, *, out: Optional[Tensor]=None, {}) -> Tensor: ...'
                   .format(FACTORY_PARAMS)],
        'randint': ['def randint(low: _int, high: _int, size: _size, *,'
                    ' generator: Optional[Generator]=None, {}) -> Tensor: ...'
                    .format(FACTORY_PARAMS),
                    'def randint(high: _int, size: _size, *,'
                    ' generator: Optional[Generator]=None, {}) -> Tensor: ...'
                    .format(FACTORY_PARAMS)],
        'full': ['def full(size: _size, fill_value: Number, *,'
                 ' out: Optional[Tensor]=None,'
                 ' layout: _layout=strided, {}) -> Tensor: ...'
                 .format(FACTORY_PARAMS),
                 'def full(size: _size, fill_value: Number, *,'
                 ' names: List[Union[str, None]],'
                 ' layout: _layout=strided, {}) -> Tensor: ...'
                 .format(FACTORY_PARAMS)],
        'is_grad_enabled': ['def is_grad_enabled() -> _bool: ...'],
        'nonzero': ['def nonzero(input: Tensor, *, out: Optional[Tensor]=None) -> Tensor: ...',
                    'def nonzero(input: Tensor, *, as_tuple: bool=...) -> Tensor: ...'],
    })
    for binop in ['mul', 'div', 'true_divide', 'floor_divide']:
        unsorted_function_hints[binop].append(
            'def {}(input: Union[Tensor, Number],'
            ' other: Union[Tensor, Number],'
            ' *, out: Optional[Tensor]=None) -> Tensor: ...'.format(binop))
    for binop in ['add', 'sub']:
        unsorted_function_hints[binop].append(
            'def {}(input: Union[Tensor, Number],'
            ' other: Union[Tensor, Number],'
            ' *, alpha: Optional[Number]=1, out: Optional[Tensor]=None) -> Tensor: ...'.format(binop))

    function_signatures = load_signatures(native_yaml_path, deprecated_yaml_path, method=False, pyi=True)
    sig_groups = get_py_torch_functions(function_signatures)
    for name in sorted(sig_groups.keys()):
        unsorted_function_hints[name] += generate_type_hints(sig_groups[name])
        # deprecated signatures are not used when computing named tuples
        native_groups = [g for g in sig_groups[name] if not g.signature.deprecated]
        namedtuples.update(generate_named_tuples(native_groups))

    function_hints = []
    for name, hints in sorted(unsorted_function_hints.items()):
        if len(hints) > 1:
            hints = ['@overload\n' + h for h in hints]
        function_hints += hints

    # Generate type signatures for Tensor methods
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    unsorted_tensor_method_hints: Dict[str, List[str]] = collections.defaultdict(list)
    unsorted_tensor_method_hints.update({
        'size': ['def size(self) -> Size: ...',
                 'def size(self, _int) -> _int: ...'],
        'stride': ['def stride(self) -> Tuple[_int]: ...',
                   'def stride(self, _int) -> _int: ...'],
        'new_ones': ['def new_ones(self, size: _size, {}) -> Tensor: ...'.
                     format(FACTORY_PARAMS)],
        'new_tensor': ["def new_tensor(self, data: Any, {}) -> Tensor: ...".format(FACTORY_PARAMS)],
        # new and __init__ have the same signatures differ only in return type
        # Adapted from legacy_tensor_ctor and legacy_tensor_new
        'new': ['def new(self, *args: Any, {}) ->Tensor: ...'.format(DEVICE_PARAM),
                'def new(self, storage: Storage) -> Tensor: ...',
                'def new(self, other: Tensor) -> Tensor: ...',
                'def new(self, size: _size, *, {}) -> Tensor: ...'.format(DEVICE_PARAM),
                ],
        '__init__': ['def __init__(self, *args: Any, {}) -> None: ...'.format(DEVICE_PARAM),
                     'def __init__(self, storage: Storage) -> None: ...',
                     'def __init__(self, other: Tensor) -> None: ...',
                     'def __init__(self, size: _size, *, {}) -> None: ...'.format(DEVICE_PARAM),
                     ],
        'as_subclass': ["def as_subclass(self, cls: Tensor) -> Tensor: ..."],
        # clamp has no default values in the Declarations
        'clamp': ["def clamp(self, min: _float=-inf, max: _float=inf,"
                  " *, out: Optional[Tensor]=None) -> Tensor: ..."],
        'clamp_': ["def clamp_(self, min: _float=-inf, max: _float=inf) -> Tensor: ..."],
        '__getitem__': ["def __getitem__(self, {}) -> Tensor: ...".format(INDICES)],
        '__setitem__': ["def __setitem__(self, {}, val: Union[Tensor, Number])"
                        " -> None: ...".format(INDICES)],
        'tolist': ['def tolist(self) -> List: ...'],
        'requires_grad_': ['def requires_grad_(self, mode: _bool=True) -> Tensor: ...'],
        'element_size': ['def element_size(self) -> _int: ...'],
        'data_ptr': ['def data_ptr(self) -> _int: ...'],
        'dim': ['def dim(self) -> _int: ...'],
        'nonzero': ['def nonzero(self, *, as_tuple: _bool=...) -> Tensor: ...'],
        'numel': ['def numel(self) -> _int: ...'],
        'ndimension': ['def ndimension(self) -> _int: ...'],
        'nelement': ['def nelement(self) -> _int: ...'],
        'cuda': ['def cuda(self, device: Optional[Union[_device, _int, str]]=None, non_blocking: _bool=False) -> Tensor: ...'],
        'numpy': ['def numpy(self) -> Any: ...'],
        'apply_': ['def apply_(self, callable: Callable) -> Tensor: ...'],
        'map_': ['def map_(self, tensor: Tensor, callable: Callable) -> Tensor: ...'],
        'storage': ['def storage(self) -> Storage: ...'],
        'type': ['def type(self, dtype: None=None, non_blocking: _bool=False) -> str: ...',
                 'def type(self, dtype: Union[str, _dtype], non_blocking: _bool=False) -> Tensor: ...',
                 ],
        'get_device': ['def get_device(self) -> _int: ...'],
        'contiguous': ['def contiguous(self, memory_format=torch.contiguous_format) -> Tensor: ...'],
        'is_contiguous': ['def is_contiguous(self, memory_format=torch.contiguous_format) -> _bool: ...'],
        'is_cuda': ['is_cuda: _bool'],
        'is_leaf': ['is_leaf: _bool'],
        'is_sparse': ['is_sparse: _bool'],
        'is_quantized': ['is_quantized: _bool'],
        'is_meta': ['is_meta: _bool'],
        'is_mkldnn': ['is_mkldnn: _bool'],
        'is_vulkan': ['is_vulkan: _bool'],
        'storage_offset': ['def storage_offset(self) -> _int: ...'],
        'to': ['def to(self, dtype: _dtype, non_blocking: _bool=False, copy: _bool=False) -> Tensor: ...',
               'def to(self, device: Optional[Union[_device, str]]=None, dtype: Optional[_dtype]=None, '
               'non_blocking: _bool=False, copy: _bool=False) -> Tensor: ...',
               'def to(self, other: Tensor, non_blocking: _bool=False, copy: _bool=False) -> Tensor: ...',
               ],
        'item': ["def item(self) -> Number: ..."],
        'copy_': ["def copy_(self, src: Tensor, non_blocking: _bool=False) -> Tensor: ..."],
        'set_': ['def set_(self, storage: Storage, offset: _int, size: _size, stride: _size) -> Tensor: ...',
                 'def set_(self, storage: Storage) -> Tensor: ...'],
        'split': ['def split(self, split_size: _int, dim: _int=0) -> Sequence[Tensor]: ...',
                  'def split(self, split_size: Tuple[_int, ...], dim: _int=0) -> Sequence[Tensor]: ...'],
    })
    for binop in ['mul', 'div', 'true_divide', 'floor_divide']:
        for inplace in [False, True]:
            out_suffix = ', *, out: Optional[Tensor]=None'
            if inplace:
                binop += '_'
                out_suffix = ''
            unsorted_tensor_method_hints[binop].append(
                'def {}(self, other: Union[Tensor, Number]{})'
                ' -> Tensor: ...'.format(binop, out_suffix))
    for binop in ['add', 'sub']:
        for inplace in [False, True]:
            out_suffix = ', out: Optional[Tensor]=None'
            if inplace:
                binop += '_'
                out_suffix = ''
            unsorted_tensor_method_hints[binop].append(
                'def {}(self, other: Union[Tensor, Number], '
                '*, alpha: Optional[Number]=1{})'
                ' -> Tensor: ...'.format(binop, out_suffix))
    simple_conversions = ['byte', 'char', 'cpu', 'double', 'float',
                          'half', 'int', 'long', 'short', 'bool',
                          'bfloat16']
    for name in simple_conversions:
        unsorted_tensor_method_hints[name].append('def {}(self) -> Tensor: ...'.format(name))

    # pyi tensor methods don't currently include deprecated signatures for some reason
    # TODO: we should probably add them in
    tensor_method_signatures = load_signatures(native_yaml_path, deprecated_yaml_path, method=True, skip_deprecated=True, pyi=True)
    tensor_method_sig_groups = get_py_torch_functions(tensor_method_signatures, method=True)

    for name in sorted(tensor_method_sig_groups.keys()):
        unsorted_tensor_method_hints[name] += generate_type_hints(tensor_method_sig_groups[name], is_tensor=True)
        namedtuples.update(generate_named_tuples(tensor_method_sig_groups[name]))

    for op in all_ops:
        name = '__{}__'.format(op)
        unsorted_tensor_method_hints[name] += sig_for_ops(name)

    tensor_method_hints = []
    for name, hints in sorted(unsorted_tensor_method_hints.items()):
        if len(hints) > 1:
            hints = ['@overload\n' + h for h in hints]
        tensor_method_hints += hints

    # TODO: Missing type hints for nn

    # Generate namedtuple definitions
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    namedtuple_defs = ['{} = {}'.format(name, defn) for name, defn in namedtuples.items()]

    # Generate type signatures for legacy classes
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    # TODO: These are deprecated, maybe we shouldn't type hint them
    legacy_storage_base_hints = []
    dt = ('Double', 'Float', 'Long', 'Int',
          'Short', 'Char', 'Byte', 'Bool',
          'Half', 'BFloat16', 'ComplexDouble',
          'ComplexFloat', 'QUInt8', 'QInt8', 'QInt32', 'QUInt4x2')
    for c in dt:
        legacy_storage_base_hints.append('class {}StorageBase(object): ...'.format(c))
    for c in dt:
        legacy_storage_base_hints.append('class Cuda{}StorageBase(object): ...'.format(c))

    legacy_class_hints = []
    for c in ('DoubleTensor', 'FloatTensor', 'LongTensor', 'IntTensor',
              'ShortTensor', 'HalfTensor', 'CharTensor', 'ByteTensor', 'BoolTensor'):
        legacy_class_hints.append('class {}(Tensor): ...'.format(c))

    # Generate type signatures for dtype classes
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    # TODO: don't explicitly list dtypes here; get it from canonical
    # source
    dtype_class_hints = ['{}: dtype = ...'.format(n)
                         for n in
                         ['float32', 'float', 'float64', 'double', 'float16', 'bfloat16', 'half',
                          'uint8', 'int8', 'int16', 'short', 'int32', 'int', 'int64', 'long',
                          'complex32', 'complex64', 'cfloat', 'complex128', 'cdouble',
                          'quint8', 'qint8', 'qint32', 'bool', 'quint4x2']]

    # Generate __all__ directive
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    # Include only the functions that contain hints, to prevent undefined
    # symbols to be included in the `__all__` directive.
    hinted_function_names = [name for name, hint in unsorted_function_hints.items() if hint]
    all_symbols = sorted(list(namedtuples.keys()) + hinted_function_names)
    all_directive = pformat(all_symbols, width=100, compact=True).split('\n')
    all_directive[0] = '__all__ = {}'.format(all_directive[0])

    # Write out the stub
    # ~~~~~~~~~~~~~~~~~~

    env = {
        'namedtuple_defs': namedtuple_defs,
        'function_hints': function_hints,
        'tensor_method_hints': tensor_method_hints,
        'legacy_class_hints': legacy_class_hints,
        'legacy_storage_base_hints': legacy_storage_base_hints,
        'dtype_class_hints': dtype_class_hints,
        'all_directive': all_directive
    }
    TORCH_C_TYPE_STUBS = CodeTemplate.from_file(os.path.join('torch', '_C', '__init__.pyi.in'))
    TORCH_C_VARIABLE_FUNCTIONS_TYPE_STUBS = \
        CodeTemplate.from_file(os.path.join('torch', '_C', '_VariableFunctions.pyi.in'))

    write(out, 'torch/_C/__init__.pyi', TORCH_C_TYPE_STUBS, env)
    write(out, 'torch/_C/_VariableFunctions.pyi', TORCH_C_VARIABLE_FUNCTIONS_TYPE_STUBS, env)
    write(out, 'torch/_VF.pyi', TORCH_C_VARIABLE_FUNCTIONS_TYPE_STUBS, env)
    gen_nn_pyi(out)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Generate type stubs for PyTorch')
    parser.add_argument('--native-functions-path', metavar='NATIVE',
                        default='aten/src/ATen/native/native_functions.yaml',
                        help='path to native_functions.yaml')
    parser.add_argument('--deprecated-functions-path', metavar='DEPRECATED',
                        default='tools/autograd/deprecated.yaml',
                        help='path to deprecated.yaml')
    parser.add_argument('--out', metavar='OUT',
                        default='.',
                        help='path to output directory')
    args = parser.parse_args()
    gen_pyi(args.native_functions_path, args.deprecated_functions_path, args.out)


if __name__ == '__main__':
    main()
