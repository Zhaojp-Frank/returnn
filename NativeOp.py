
"""
Generic interface which automatically creates:
* CPU and GPU op
* inplace and not inplace
* grad variants
"""

import copy
import os
import sys

import numpy
import theano
import theano.sandbox.cuda
import theano.tensor as T
from theano.compile import optdb
from theano import gof
from theano.gof.opt import OpSub

from Util import make_hashable, make_dll_name, escape_c_str
from TheanoUtil import try_register_gpu_opt, make_var_tuple, softmax


PY3 = sys.version_info[0] >= 3

if PY3:
  unicode = str
  long = int


class NativeOpBaseMixin(object):
  """
  The purpose of having this as a separate base class is to make this independent of any Theano specific
  functionality so that we can also use this base for example for TensorFlow.
  """

  def __init__(self, in_info, out_info,
               c_fw_code, c_bw_code=None, c_extra_support_code=None, code_version=None, cpu_support=True,
               grad_input_map=None, name=None):
    """
    :param list[dict(str)] in_info: each dict describes one input var.
      attribs in the dict:
        int ndim: the ndim.
        tuple shape: tuple and can contain None for specific dimensions.
      optional attribs:
        str dtype: "float32" by default.
        bool need_contiguous: false by default.
        int want_inplace: -1 by default. try to optimize to destroy input, on output-index.
          "dummy_out" is a special value which will add another output.
        bool is_inplace: false by default. whether the optimization was applied.
        str gradient: can be "disconnected". see grad().
        bool bw_input: True by default. add this param to the bw input.
      other attribs are just ignored.
    :param list[dict(str)] out_info: like in_info.
      slightly different behavior for:
        shape: we also allow refs to the in_info in the form (in-idx,dim). see infer_shape().
        need_contiguous/want_inplace: used for bw, in case for bw_input == True.
    :param str c_fw_code: C code for forward pass
    :param str|dict[str] c_extra_support_code: C support code (for c_support_code)
    :param str|None c_bw_code: C code for backward pass (for gradient)
    :param tuple[int] code_version: will be returned by c_code_cache_version.
    :param bool cpu_support:
    :param tuple[int]|callable grad_input_map: selection of grad inputs.
      by default, we get all inputs + all outputs + all grad outputs.
    :param str name: name
    """
    assert isinstance(in_info, (list, tuple))
    assert isinstance(out_info, (list, tuple))
    in_info, out_info, num_dummy_outs = self._resolve_want_inplace_dummy(in_info, out_info)
    self.in_info = make_hashable(in_info)
    self.out_info = make_hashable(out_info)
    self.num_dummy_outs = num_dummy_outs
    self.c_fw_code = c_fw_code
    self.c_bw_code = c_bw_code
    self.c_extra_support_code = self._reduce_c_extra_support_code(c_extra_support_code)
    self.code_version = code_version or ()
    self.cpu_support = cpu_support
    self.name = name or "<anonNativeOp>"
    self.grad_input_map = self._convert_grad_input_map(grad_input_map, len(in_info) + len(out_info) * 2)
    self.destroy_map = self._construct_destroy_map(in_info)

  @classmethod
  def _resolve_want_inplace_dummy(cls, in_info, out_info):
    in_info = [dict(info) for info in in_info]  # deep copy, don't modify original
    out_info = list(out_info)  # copying list is enough here
    num_dummy_outs = 0
    for in_idx, info in enumerate(in_info):
      if info.get("want_inplace", None) == "dummy_out":
        num_dummy_outs += 1
        dummy_out_idx = len(out_info)
        dummy_out = {"ndim": info["ndim"],
                     "shape": [(in_idx, i) for i in range(info["ndim"])],
                     "dtype": info.get("dtype", "float32"),
                     "name": "dummy_out_%i" % num_dummy_outs}
        out_info += [dummy_out]
        info["want_inplace"] = dummy_out_idx
    return in_info, out_info, num_dummy_outs

  @classmethod
  def _reduce_c_extra_support_code(cls, c):
    if c is None:
      return ""
    if isinstance(c, dict):
      c = [v for (k, v) in sorted(c.items())]
    if isinstance(c, (list, tuple)):
      c = "\n".join([v + "\n\n" for v in c])
    assert isinstance(c, (str, unicode))
    return c

  @classmethod
  def _construct_destroy_map(cls, in_info):
    destroy_map = {}
    for in_idx, info in enumerate(in_info):
      want_inplace = info.get("want_inplace", -1)
      assert isinstance(want_inplace, (int, long))
      if want_inplace >= 0 and info.get("is_inplace", False):
        out_idx = want_inplace
        # http://deeplearning.net/software/theano/extending/inplace.html
        # https://github.com/Theano/Theano/issues/3506
        # It's strange that we must mark which output operates on which input -
        # I would expect that it must only know which inputs are destroyed.
        assert out_idx not in destroy_map, "Theano cannot handle that yet"
        destroy_map[out_idx] = [in_idx]
    return destroy_map

  @classmethod
  def _convert_grad_input_map(cls, gi_map, num_params):
    """
    :param gi_map: see grad_input_map argument for self.__init__
    :param int num_params:
    :return: tuple of int
    :rtype: tuple[int]
    """
    if gi_map is None:
      gi_map = tuple(range(num_params))
    if callable(gi_map):
      gi_map = gi_map(*range(num_params))
    if isinstance(gi_map, list):
      gi_map = tuple(gi_map)
    assert isinstance(gi_map, tuple)
    return gi_map

  def _filter_grad_inputs(self, inputs):
    """
    :param list[T] inputs: inputs + outputs + output_grads. can be either symbolic tensors or info dicts
    :return: filtered list, via self.grad_input_map
    :rtype: list[T]
    """
    assert len(inputs) == len(self.in_info) + len(self.out_info) * 2
    return [inputs[i] for i in self.grad_input_map]

  def infer_shape(self, node, input_shapes):
    assert len(input_shapes) == len(self.in_info)
    out_shapes = []
    for info in self.out_info:
      out_shape = list(info["shape"])
      for idx, s in enumerate(out_shape):
        if isinstance(s, tuple):  # we interpret this as a reference to input shapes
          assert len(s) == 2, "dim %r invalid in info %r" % (s, info)
          assert 0 <= s[0] < len(input_shapes), "dim %r invalid in info %r" % (s, info)
          assert 0 <= s[1] < self.in_info[s[0]]["ndim"], "dim idx %r invalid in input %i %r, info %r" % (s[1], s[0], self.in_info[s[0]], info)
          out_shape[idx] = input_shapes[s[0]][s[1]]
      assert not any([s is None for s in out_shape]), "out_shape %r, out_info %r" % (out_shape, self.out_info)
      out_shapes += [tuple(out_shape)]
    return out_shapes

  @classmethod
  def _bw_in_var_info(cls, info):
    """
    :param dict[str] info:
    :return: updated info dict for the gradient (bwd) as input
    :rtype: dict[str]
    """
    if "bw_in_var" in info:
      info = dict(info)
      info.update(info.pop("bw_in_var"))
    return info

  @classmethod
  def _bw_grad_var_info(cls, info):
    """
    :param dict[str] info: backward gradient input for one of our outputs
    :return: updated info dict for the gradient (bwd) as input
    :rtype: dict[str]
    """
    info = dict(info)
    if "bw_grad_var" in info:
      info.update(info.pop("bw_grad_var"))
    if "name" in info:
      info["name"] = "D_" + info["name"]
    return info

  def kwargs_for_grad_op(self):
    """
    :returns: the kwargs for creating a NativeOp for the gradient op. e.g. includes in_info, out_info, etc
    :rtype: dict[str]

    Note: The inputs of the gradient are by default: fwd_op.inputs + fwd_op.outputs + output_grads.
    We filter them via self._filter_grad_inputs.
    """
    # Inputs: inputs + outputs + output_grads, where outputs = op(inputs),
    # i.e. we might reuse some of the calculation.
    in_info = [self._bw_in_var_info(info) for info in self.in_info]
    in_info += [self._bw_in_var_info(info) for info in self.out_info]
    in_info += [self._bw_grad_var_info(info) for info in self.out_info]
    in_info = self._filter_grad_inputs(in_info)
    in_idx_rev = {v: k for (k, v) in enumerate(self.grad_input_map)}
    # Outputs: All like original inputs. Filter our the disconnected.
    out_info = [info.copy() for info in self.in_info]
    for idx, info in enumerate(out_info):
      info.pop("shape")
      if "bw_out_var" in info:
        info.update(info["bw_out_var"])
      if "shape" not in info:
        # Refer to input shapes. See infer_shape().
        info["shape"] = [(in_idx_rev[idx], i) for i in range(info["ndim"])]
    out_info = [info for info in out_info if info.get("gradient", "") != "disconnected"]

    return dict(
      name="GradOf%s" % self.name,
      in_info=in_info,
      out_info=out_info,
      c_fw_code=self.c_bw_code,
      c_extra_support_code=self.c_extra_support_code,
      code_version=self.code_version,
      cpu_support=self.cpu_support
    )

  def make_results_of_gradient(self, grad_op_outputs, disconnected_type=None):
    """
    :param list[T] grad_op_outputs: this is already with dummy outputs removed
    :param S disconnected_type:
    :return: gradient for each input of our op
    :rtype: list[T|S]
    """
    if disconnected_type is None:
      disconnected_type = lambda: None
    grad_op_outputs = list(grad_op_outputs)
    results = []
    for info in self.in_info:
      if info.get("gradient", "") == "disconnected":
        results += [disconnected_type()]
      else:
        results += grad_op_outputs[:1]
        grad_op_outputs = grad_op_outputs[1:]
    assert len(grad_op_outputs) == 0
    assert len(results) == len(self.in_info)
    return results



class NativeOp(theano.Op, NativeOpBaseMixin):
  """
  We wrap some C code which can define a forward pass
  and optionally a backward pass (for gradient calculation).
  The C code should be Numpy and CUDA compatible. See NativeOp.cpp.
  We also support inplace operations, i.e. we can operate inplace on some inputs.
  You can define in a flexible way all the inputs and the outputs.
  See __init__() for the details.

  All output variables are created automatically with the right shape
   but their content is not initialized,
   except when its used by some input variable as the inplace output
   - in that case, it is either the input variable or it has a copy of its data.
  """

  __props__ = ("in_info", "out_info",
               "c_fw_code", "c_bw_code", "c_extra_support_code", "code_version",
               "grad_input_map", "name",
               "custom_grad")

  def __init__(self, custom_grad=None, **kwargs):
    """
    :param function custom_grad: if given, will use this instead for self.grad
    :param dict[str] kwargs: all passed to NativeOpBaseMixin
    """
    theano.Op.__init__(self)
    NativeOpBaseMixin.__init__(self, **kwargs)
    self.custom_grad = custom_grad

  def __str__(self):
    return "%s{%s,%s}" % (
      self.__class__.__name__,
      self.name,
      "inplace" if self.destroy_map else "no_inplace")

  @classmethod
  def as_tensor_var(cls, v):
    return theano.tensor.as_tensor_variable(v)

  @classmethod
  def tensor_type(cls, dtype, ndim):
    return T.TensorType(dtype=dtype, broadcastable=(False,) * ndim)

  @classmethod
  def contiguous(cls, v):
    from TheanoUtil import Contiguous
    assert isinstance(v, theano.Variable)
    if getattr(v, 'owner', None):
      assert isinstance(v.owner, theano.Apply)
      if isinstance(v.owner.op, Contiguous.__base__):
        return v
    return Contiguous()(v)

  def _convert_input_var(self, v, info):
    v = self.as_tensor_var(v)
    dtype = "float32"  # Theano on GPU only supports float32 ... # info.get("dtype", "float32")
    if v.dtype != dtype:
      v = T.cast(v, dtype)
    if v.ndim != info["ndim"]:
      raise TypeError("input var ndim %i does not match with info %r" % (v.ndim, info))
    if info.get("need_contiguous", False):
      v = self.contiguous(v)
    return v

  def grad(self, inputs, output_grads):
    if self.custom_grad:
      return self.custom_grad(self, inputs, output_grads)

    if not self.c_bw_code:
      # Unknown how to calculate gradient.
      return [T.DisconnectedType()() for inp in inputs]

    assert len(self.in_info) == len(inputs)
    assert len(self.out_info) == len(output_grads)

    # Some of output_grads might be of disconnected type.
    out_shapes = self.infer_shape(None, [v.shape for v in inputs])
    assert len(out_shapes) == len(output_grads)
    for i, out_grad in enumerate(output_grads):
      if isinstance(out_grad.type, T.DisconnectedType):
        output_grads[i] = T.zeros(out_shapes[i], dtype="float32")

    kwargs_for_grad = self.kwargs_for_grad_op()
    grad_op = self.__class__(**kwargs_for_grad)

    grad_inputs = inputs + list(make_var_tuple(self(*inputs))) + output_grads
    grad_inputs = self._filter_grad_inputs(grad_inputs)
    assert len(grad_op.in_info) == len(grad_inputs)
    grad_outputs = make_var_tuple(grad_op(*grad_inputs))
    assert len(grad_op.out_info) == len(grad_outputs)
    if grad_op.num_dummy_outs > 0:
      grad_outputs = grad_outputs[:-grad_op.num_dummy_outs]  # remove any dummy outputs

    def print_fn(op, x):
      import numpy
      first = x[(0,) * x.ndim]
      stats = (first, x.shape, numpy.min(x), numpy.max(x), numpy.mean(x), numpy.std(x),
               numpy.isinf(x).any(), numpy.isnan(x).any())
      print(op.message, "first/shape/min/max/mean/std/any-inf/any-nan:", stats)
    #input_grads = [theano.printing.Print("in grad %i" % i, global_fn=print_fn)(v)
    #               for (i, v) in enumerate(input_grads)]

    return self.make_results_of_gradient(grad_outputs, disconnected_type=T.DisconnectedType())

  def connection_pattern(self, node):
    assert len(node.inputs) == len(self.in_info)
    pattern = [[info.get("gradient", "") != "disconnected"] * len(self.out_info)
               for info in self.in_info]
    return pattern

  def make_node(self, *args):
    assert len(args) == len(self.in_info)
    args = [self._convert_input_var(arg, info) for arg, info in zip(args, self.in_info)]
    outputs = [self.tensor_type(dtype=info.get("dtype", "float32"), ndim=info["ndim"])()
               for info in self.out_info]
    return theano.Apply(self, args, outputs)

  def perform(self, node, inputs, output_storage):
    raise NotImplementedError("NativeOp: no pure Python implementation, only C implementation")

  def c_code_cache_version(self):
    return self.code_version

  def c_support_code(self):
    base_src = open(os.path.dirname(__file__) + "/NativeOp.cpp").read()
    return "\n\n".join([
      T.blas.blas_header_text(),
      "#define CUDA 0",
      base_src,
      self.c_extra_support_code])

  def c_libraries(self):
    return T.blas.ldflags()

  def c_compile_args(self):
    return T.blas.ldflags(libs=False, flags=True)

  def c_lib_dirs(self):
    return T.blas.ldflags(libs=False, libs_dir=True)

  def c_header_dirs(self):
    return T.blas.ldflags(libs=False, include_dir=True)

  def c_code(self, node, name, inputs, outputs, sub):
    assert len(inputs) == len(self.in_info)
    assert len(outputs) == len(self.out_info)
    return """
    {
      int n_inputs = %(n_inputs)i, n_outputs = %(n_outputs)i;
      Ndarray* inputs[] = {%(input_var_names_str)s};
      Ndarray** outputs[] = {%(output_var_names_str)s};
      int in_ndims[] = {%(input_ndims_str)s};
      int out_ndims[] = {%(output_ndims_str)s};
      Ndarray_DIM_Type output_shapes_flat[] = {%(output_shapes_flat_str)s};
      int in_want_inplace[] = {%(input_want_inplace_str)s};
      bool in_is_inplace[] = {%(input_is_inplace_str)s};

      // Check if we can reuse any preallocated output.
      // Reset those which we cannot reuse.
      {
        int out_shape_idx = 0;
        for(int i = 0; i < n_outputs; ++i) {
          assert_cmp(out_shape_idx + out_ndims[i], <=, ARRAY_LEN(output_shapes_flat));
          if(*outputs[i]) {
            bool can_reuse = true;
            for(int j = 0; j < out_ndims[i]; ++j)
              if(output_shapes_flat[out_shape_idx + j] != Ndarray_DIMS(*outputs[i])[j]) {
                can_reuse = false;
                break;
              }
            if(!can_reuse)
              Py_CLEAR(*outputs[i]);
          }
          out_shape_idx += out_ndims[i];
        }
        assert_cmp(out_shape_idx, ==, ARRAY_LEN(output_shapes_flat));
      }

      // Maybe reuse or otherwise copy input into output vars.
      for(int i = 0; i < n_inputs; ++i)
        if(in_want_inplace[i] >= 0) {
          assert_cmp(in_want_inplace[i], <, n_outputs);
          Py_XDECREF(*outputs[in_want_inplace[i]]);
          if(in_is_inplace[i]) {
            *(outputs[in_want_inplace[i]]) = inputs[i];
            Py_INCREF(inputs[i]);
          } else {
            *(outputs[in_want_inplace[i]]) = (Ndarray*) Ndarray_Copy(inputs[i]);
            if(!*(outputs[in_want_inplace[i]])) %(fail)s;
            inputs[i] = *(outputs[in_want_inplace[i]]);  // reset with copy
          }
        }

      // Init the remaining output vars. Note that they are initialized randomly!
      {
        int out_shape_idx = 0;
        for(int i = 0; i < n_outputs; ++i) {
          assert(out_shape_idx + out_ndims[i] <= ARRAY_LEN(output_shapes_flat));
          if(*(outputs[i])) {
            for(int j = 0; j < out_ndims[i]; ++j)
              // If this fails, we maybe have reused an input which has an invalid shape.
              assert_cmp(output_shapes_flat[out_shape_idx + j], ==, Ndarray_DIMS(*outputs[i])[j]);
          }
          else {
            *(outputs[i]) = (Ndarray*) Ndarray_NewDims(out_ndims[i], &output_shapes_flat[out_shape_idx]);
            if(!*(outputs[i])) %(fail)s;
          }
          out_shape_idx += out_ndims[i];
        }
        assert_cmp(out_shape_idx, ==, ARRAY_LEN(output_shapes_flat));
      }

      // And the user C code starts here.
      // --------------------------------
      %(c_code)s;
    }
    """ % {
      'name': name, 'fail': sub['fail'],
      'op_name': escape_c_str(self.name),
      'c_code': self.c_fw_code % {'fail': sub['fail']},
      'n_inputs': len(inputs), 'n_outputs': len(outputs),
      'input_var_names_str': ", ".join(["%s" % inp for inp in inputs]),
      'output_var_names_str': ", ".join(["&%s" % out for out in outputs]),
      'input_ndims_str': ', '.join(["%i" % info["ndim"] for info in self.in_info]),
      'output_ndims_str': ', '.join(["%i" % info["ndim"] for info in self.out_info]),
      'output_shapes_flat_str':
        ', '.join([(("%i" % s) if isinstance(s, (int, long))
                    else "Ndarray_DIMS(inputs[%i])[%i]" % s)
                   for info in self.out_info for s in info["shape"]]),
      "input_want_inplace_str": ", ".join([str(int(info.get("want_inplace", -1)))
                                           for info in self.in_info]),
      "input_is_inplace_str": ", ".join([str(int(info.get("is_inplace", False)))
                                         for info in self.in_info])
    }


class GpuNativeOp(NativeOp, theano.sandbox.cuda.GpuOp):

  @classmethod
  def as_tensor_var(cls, v):
    from theano.sandbox.cuda.basic_ops import as_cuda_ndarray_variable
    return as_cuda_ndarray_variable(v)

  @classmethod
  def tensor_type(cls, dtype, ndim):
    from theano.sandbox.cuda import CudaNdarrayType
    if dtype != "float32":
      print("%s: WARNING: cannot handle type %r, will use float32 instead" % ("GpuNativeOp", dtype))
      dtype = "float32"
    return CudaNdarrayType(dtype=dtype, broadcastable=(False,) * ndim)

  @classmethod
  def contiguous(cls, v):
    from theano.sandbox.cuda.basic_ops import gpu_contiguous
    assert isinstance(v, (theano.sandbox.cuda.CudaNdarrayVariable, theano.sandbox.cuda.CudaNdarrayConstant))
    if getattr(v, 'owner', None):
      assert isinstance(v.owner, theano.Apply)
      if v.owner.op == gpu_contiguous:
        return v
    return gpu_contiguous(v)

  def c_support_code(self):
    src = open(os.path.dirname(__file__) + "/NativeOp.cpp").read()
    return "\n\n".join([
      "#define CUDA 1",
      src,
      self.c_extra_support_code,
      "// end of c_support_code\n\n\n"])


@gof.local_optimizer([NativeOp], inplace=True)
def inplace_NativeOp(node):
  if isinstance(node.op, NativeOp) and not node.op.destroy_map:
    kwargs = {k: getattr(node.op, k) for k in node.op.__props__}
    # TODO: We could try to make each input inplace individually.
    # What we do now is just to try to make all inplace.
    kwargs["in_info"] = [dict(info) for info in node.op.in_info]
    any_inplace = False
    for info in kwargs["in_info"]:
      if info.get("want_inplace", -1) >= 0:
        any_inplace = True
        info["is_inplace"] = True
    if not any_inplace:
      return False
    new_op = node.op.__class__(**kwargs)
    from TheanoUtil import make_var_tuple
    new_v = make_var_tuple(new_op(*node.inputs))
    return new_v
  return False

try:
  optdb.register('inplace_NativeOp',
                 gof.TopoOptimizer(inplace_NativeOp
                                   , failure_callback=gof.TopoOptimizer.warn_inplace
                                   ),
                 60, 'fast_run', 'inplace')
except ValueError:  # can happen if it was already registered before, e.g. when we reload the module
  pass


@try_register_gpu_opt(NativeOp)
def local_gpu_NativeOp(node):
  if isinstance(node.op, NativeOp):
    # see also: https://github.com/Theano/Theano/blob/master/theano/sandbox/cuda/opt.py
    from theano.sandbox.cuda import host_from_gpu, gpu_from_host, as_cuda_ndarray_variable
    args = node.inputs
    if any([(x.owner and x.owner.op == host_from_gpu) for x in args]):
      gpu_op = GpuNativeOp(**{key: getattr(node.op, key) for key in node.op.__props__})
      args = [x.owner.inputs[0] if (x.owner and x.owner.op == host_from_gpu) else x
              for x in args]
      from TheanoUtil import make_var_tuple
      outputs = make_var_tuple(gpu_op(*args))
      return [host_from_gpu(out) for out in outputs]


class NativeOpGenBase:
  """
  Base interface for op generation.
  See NativeOp.__init__() for attribs.
  """
  in_info = None  # type: tuple[dict[str]]
  out_info = None  # type: tuple[dict[str]]
  c_fw_code = None  # type: str
  c_bw_code = None  # type: str
  c_extra_support_code = None  # type: dict[str,str]
  code_version = None  # type: tuple[int]|int
  grad_input_map = None
  custom_grad = None
  cpu_support = True

  def make_op(self):
    assert self.in_info is not None
    assert self.out_info is not None
    assert self.c_fw_code is not None
    return NativeOp(in_info=self.in_info, out_info=self.out_info,
                    c_fw_code=self.c_fw_code, c_bw_code=self.c_bw_code,
                    c_extra_support_code=self.c_extra_support_code,
                    grad_input_map=self.grad_input_map,
                    name=self.__class__.__name__,
                    custom_grad=self.custom_grad)

  @classmethod
  def map_layer_inputs_to_op(cls, *inputs):
    return inputs

  @classmethod
  def map_layer_output_from_op(cls, *outputs):
    return outputs[0]


class LstmGenericBase(NativeOpGenBase):
  """
  inputs:
    :param Z: {input,output,forget} gate + cell state. 3d (time,batch,dim*4)
    :param V_h: recurrent matrix. 2d (dim,dim*4)
    :param c: initial cell state. 2d (batch,dim)
    :param i: index. 2d (time,batch) -> 0 or 1
  outputs:
    :param Y: output. 3d (time,batch,dim)
    :param H: gates and cell state. 3d (time,batch,dim*4)
    :param d: final cell state. 2d (batch,dim)
  """
  in_info = (
    {"name": "Z", "ndim": 3, "shape": (None, None, None), "need_contiguous": True,
     "want_inplace": 1,
     "bw_out_var": {"shape": ((2, 0), (2, 1), (0, 1))}},  # see grad_input_map() for indices
    {"name": "V_h", "ndim": 2, "shape": (None, None), "need_contiguous": True},
    {"name": "c", "ndim": 2, "shape": (None, None), "need_contiguous": True},
    {"name": "i", "ndim": 2, "shape": (None, None), "need_contiguous": True,
     "gradient": "disconnected"}
  )
  out_info = (
    {"name": "Y", "ndim": 3, "shape": ((0, 0), (0, 1), (1, 0)), "need_contiguous": True,
     "bw_grad_var": {"want_inplace": "dummy_out"}},
    {"name": "H", "ndim": 3, "shape": ((0, 0), (0, 1), (0, 2)), "need_contiguous": True,
     "bw_in_var": {"want_inplace": 0}},
    {"name": "d", "ndim": 2, "shape": ((2, 0), (2, 1)), "need_contiguous": True}
  )
  @classmethod
  def grad_input_map(cls, Z, V_h, c, i,  Y, H, d,  DY, DH, Dd):
    return (V_h, c, i,  Y, H,  DY, Dd)

  @classmethod
  def map_layer_inputs_to_op(cls, Z, V_h, i):
    assert Z.ndim == 3
    assert V_h.ndim == 2
    assert i.ndim == 2
    n_batch = Z.shape[1]
    n_out = V_h.shape[0]
    c = T.zeros((n_batch, n_out), dtype="float32")
    return Z, V_h, c, i

  c_extra_support_code = {
    "lstm_kernel": """
      DEF_KERNEL
      void lstm_kernel(float* data, const float* old_state, bool old_state_strided,
                       float* output, float* state_out, int n_cells, int n_batch, const float* i) {
        //layout:
        //data[0*n_cells..1*n_cells-1] : input gate
        //data[1*n_cells..2*n_cells-1] : forget gate
        //data[2*n_cells..3*n_cells-1] : output gate
        //data[3*n_cells..4*n_cells-1] : cell state
        //output[0*n_cells..1*n_cells-1]: cell output
        //repeated for every mini-batch

        int idx = threadIdx.x + blockDim.x * blockIdx.x;
        while (idx < n_cells * n_batch) {
          int batch_idx = idx / n_cells;
          int start = batch_idx * 4 * n_cells + idx % n_cells;
          float i_batch = i[batch_idx];

          //input, forget and output gates
          float inpGate = 1.f / (1.f + expf(-data[start + n_cells]));
          float fgtGate = 1.f / (1.f + expf(-data[start + 2 * n_cells]));
          float outGate = 1.f / (1.f + expf(-data[start + 3 * n_cells]));
          float state = inpGate * tanhf(data[start]);
          float old_state_batch = old_state_strided ? old_state[start] : old_state[idx];

          state += fgtGate * old_state_batch;
          state = state * i_batch + old_state_batch * (1.f - i_batch);

          //cell output
          output[idx] = outGate * tanhf(state) * i_batch;

          data[start] = state;
          data[start + n_cells] = inpGate;
          data[start + 2 * n_cells] = fgtGate;
          data[start + 3 * n_cells] = outGate;
          if(state_out)
            state_out[idx] = state;

          idx += gridDim.x * blockDim.x;
        }
      }
    """,
    "lstm_bwd_kernel": """
      DEF_KERNEL
      void lstm_bwd_kernel(
            float* delta, float* epsilon, const float* next_epsilon, const float* old_state,
            bool old_state_strided, const float* Y, int n_cells, int n_batch, const float* i) {
        //layout:
        //delta[0*n_cells..1*n_cells-1] : input gate
        //delta[1*n_cells..2*n_cells-1] : forget gate
        //delta[2*n_cells..3*n_cells-1] : output gate
        //delta[3*n_cells..4*n_cells-1] : cell state
        //epsilon[0*n_cells..1*n_cells-1]: cell output derivative (later overwritten, see below)
        //next_epsilon[0*n_cells..1*n_cells-1]: cell state derivative * forget_gate (of next timestep)
        //repeated for every mini-batch

        int idx = threadIdx.x + blockDim.x * blockIdx.x;
        while (idx < n_cells * n_batch) {
          int batch_idx = idx / n_cells;
          int batch_offset = batch_idx * 4 * n_cells;
          int cell_offset = idx % n_cells;
          int start = batch_offset + cell_offset;
          float i_batch = i[batch_idx];

          float inpGate = delta[start + n_cells];
          float fgtGate = delta[start + 2 * n_cells];
          float outGate = delta[start + 3 * n_cells];
          float oldState = old_state_strided ? old_state[start] : old_state[idx];
          float state = delta[start];
          float eps = epsilon[idx];

          //avoid division by 0
          float gc = tanhf(state); //g(c(t))
          float gzc = (state - fgtGate * oldState) / fmaxf(inpGate, float(1e-16)); //g(z_c(t))

          //delta_output
          delta[start + 3 * n_cells] = outGate * (1.f - outGate) * gc * eps * i_batch;

          //epsilon_c
          float epsilon_c = (1.f - (gc * gc)) * outGate * eps;
          epsilon_c += next_epsilon[idx];
          epsilon[idx] = epsilon_c * fgtGate * i_batch + next_epsilon[idx] * (1.f - i_batch);

          //delta_cell
          delta[start] = inpGate * (1.f - (gzc * gzc)) * epsilon_c * i_batch;

          //delta_forget
          delta[start + 2 * n_cells] = fgtGate * (1.f - fgtGate) * oldState * epsilon_c * i_batch;

          //delta_input
          delta[start + n_cells] = inpGate * (1.f - inpGate) * gzc * epsilon_c * i_batch;

          idx += gridDim.x * blockDim.x;
        }
      }
      """
  }

  c_fw_code = """
    // Z*, V_h, c, i = input_names (*: inplace)
    // Y, H, d = output_names
    assert(n_inputs == 4);
    assert(n_outputs == 3);
    Ndarray* V_h = inputs[1];
    Ndarray* c = inputs[2];
    Ndarray* i = inputs[3];
    Ndarray* Y = *outputs[0];
    Ndarray* H = *outputs[1]; // inplace on Z
    Ndarray* d = *outputs[2];

    long T = Ndarray_DIMS(i)[0];
    int n_batch = Ndarray_DIMS(i)[1];
    assert(Ndarray_DIMS(H)[2] %% 4 == 0); // 3 gates + cell
    int n_cells = Ndarray_DIMS(H)[2] / 4;

    assert(T > 0);
    for(int x = 0; x < T; ++x) {
      if(x > 0) {
        //H += Y[x-1]*V_h
        affine_y_x(x-1, Y,  x, V_h,  x, H);
      }

      start_dev_kernel(lstm_kernel, (
        data_ptr(H, x),
        x > 0 ? data_ptr(H, x - 1) : Ndarray_DEV_DATA(c),
        x > 0,
        data_ptr(Y, x),
        (x == T - 1) ? Ndarray_DEV_DATA(d) : 0,
        n_cells,
        n_batch,
        Ndarray_DEV_DATA(i) + x * n_batch
      ));
    }
  """

  c_bw_code = """
    // V_h, c, i,   Y, H*,   DY*, Dd = input_names (*: inplace)
    // DZ, DV_h, Dc, tmpDc = output_names
    assert(n_inputs == 7);
    assert(n_outputs == 4);
    Ndarray* V_h = inputs[0];
    Ndarray* c = inputs[1];
    Ndarray* i = inputs[2];
    Ndarray* Y = inputs[3];
    Ndarray* Dd = inputs[6];
    Ndarray* DZ = *outputs[0]; // inplace on H
    Ndarray* DV_h = *outputs[1];
    Ndarray* Dc = *outputs[2];
    Ndarray* tmpDc = *outputs[3]; // (old DY), inplace buffer

    long T = Ndarray_DIMS(i)[0];
    int n_batch = Ndarray_DIMS(i)[1];
    assert(Ndarray_DIMS(DZ)[2] %% 4 == 0); // 3 gates + cell
    int n_cells = Ndarray_DIMS(DZ)[2] / 4;

    assert(T > 0);
    for(int x = T - 1; x >= 0; --x) {
      // add recurrent
      bool rightBorder = (x == T - 1);
      if(!rightBorder)
        affine_y_x(x+1, DZ,  x, V_h,  x, tmpDc,  false, true);

      start_dev_kernel(lstm_bwd_kernel, (
        data_ptr(DZ, x),
        data_ptr(tmpDc, x),
        rightBorder ? Ndarray_DEV_DATA(Dd) : data_ptr(tmpDc, x + 1),
        x > 0 ? data_ptr(DZ, x - 1) : Ndarray_DEV_DATA(c),
        x > 0,
        data_ptr(Y, x),
        n_cells,
        n_batch,
        Ndarray_DEV_DATA(i) + x * n_batch
      ));
    }

    //DV_h = Y[0..end-1]^T * DZ[1..end]
    affine_global(Y, DZ, DV_h, true, false, 1, 0.0f);

    const Ndarray_DIM_Type* Dc_dim = Ndarray_HOST_DIMS(Dc);
    Ndarray_memcpy(
      Ndarray_DEV_DATA(Dc), Ndarray_DEV_DATA(tmpDc),
      Dc_dim[0] * Dc_dim[1] * sizeof(float));

  """

  code_version = ()


class LstmLowMem(NativeOpGenBase):
  """
  inputs:
    :param X: (time,batch,in_dim)
    :param W: forward+recurrent matrix. 2d (in_dim+dim,dim*4)
    :param b: bias. 1d (dim*4,)
    :param y0: initial output|hidden state. 2d (batch,dim)
    :param c0: initial cell state. 2d (batch,dim)
    :param i: index. 2d (time,batch) -> 0 or 1
    :param start: where to start. must be >=0, default is usually 0. dtype int, scalar.
    :param step: +1 for fwd, -1 for bwd direction. can also be |step|>1 for wider steps. dtype int, scalar.
      for bwd (<0), will start at T-start-1.
  outputs:
    :param Y: output. 3d (time,batch,dim)
    :param C: cell states. 3d (time,batch,dim). gradient ignored!
    :param d: final cell state. 2d (batch,dim)
  """
  in_info = (
    {"name": "X", "ndim": 3, "shape": (None, None, None), "need_contiguous": True},
    {"name": "W", "ndim": 2, "shape": (None, None), "need_contiguous": True},
    {"name": "b", "ndim": 1, "shape": (None,), "need_contiguous": True},
    {"name": "y0", "ndim": 2, "shape": (None, None), "need_contiguous": True},
    {"name": "c0", "ndim": 2, "shape": (None, None), "need_contiguous": True},
    {"name": "i", "ndim": 2, "shape": (None, None), "need_contiguous": True, "gradient": "disconnected"},
    {"name": "start", "ndim": 0, "shape": (), "gradient": "disconnected", "dtype": "int32", "host_memory": True},
    {"name": "step", "ndim": 0, "shape": (), "gradient": "disconnected", "dtype": "int32", "host_memory": True},
  )
  out_info = (
    {"name": "Y", "ndim": 3, "shape": ((0, 0), (0, 1), (4, 1)), "need_contiguous": True},
    {"name": "C", "ndim": 3, "shape": ((0, 0), (0, 1), (4, 1)), "need_contiguous": True},
    {"name": "d", "ndim": 2, "shape": ((0, 1), (4, 1)), "need_contiguous": True}
  )
  @classmethod
  def grad_input_map(cls, X, W, b, y0, c0, i, start, step,   Y, C, d,   DY, DC, Dd):
    return (X, W, b, y0, c0, i, start, step,   Y, C,   DY, Dd)

  c_extra_support_code = {
    "lstm_kernel": """
      DEF_KERNEL
      void lstm_kernel(
        int n_batch, int n_cells, const float* mask,
        float* intern,
        float* prev_c,
        float* y,
        float* c)
      {
        int idx = threadIdx.x + blockDim.x * blockIdx.x;
        while (idx < n_cells * n_batch) {
          int batch_idx = idx / n_cells;
          int cell_idx = idx % n_cells;
          int intern_offset = batch_idx * 4 * n_cells + cell_idx;
          float prev_c_b = prev_c[idx];
          float mask_b = mask[batch_idx];

          // cell-in + input, forget and output gates
          float cellIn = tanhf(intern[intern_offset]);
          float inpGate = 1.f / (1.f + expf(-intern[intern_offset + n_cells]));
          float fgtGate = 1.f / (1.f + expf(-intern[intern_offset + 2 * n_cells]));
          float outGate = 1.f / (1.f + expf(-intern[intern_offset + 3 * n_cells]));

          float c_b = (prev_c_b * fgtGate + cellIn * inpGate) * mask_b
                      + prev_c_b * (1.f - mask_b);
          c[idx] = c_b;
          y[idx] = tanhf(c_b) * outGate * mask_b;

          idx += gridDim.x * blockDim.x;
        }
      }
      """,
    "lstm_bwd_kernel": """
      DEF_KERNEL
      void lstm_bwd_kernel(
        int n_batch, int n_in, int n_cells, const float* mask,
        float* x_h,
        float* intern,
        float* prev_c,
        float* y,
        float* c,
        float* d_y,
        float* d_h,
        float* d_c,
        float* d_intern,
        float* d_b)
      {
        int idx = threadIdx.x + blockDim.x * blockIdx.x;
        while (idx < n_cells * n_batch) {
          int batch_idx = idx / n_cells;
          int cell_idx = idx % n_cells;
          int intern_offset = batch_idx * 4 * n_cells + cell_idx;
          float mask_b = mask[batch_idx];
          float d_y_b = d_y[idx] * mask_b + d_h[idx];
          float d_c_b = d_c[idx] * mask_b;
          float prev_c_b = prev_c[idx];

          // cell-in + input, forget and output gates
          float cellIn = tanhf(intern[intern_offset]);
          float inpGate = 1.f / (1.f + expf(-intern[intern_offset + n_cells]));
          float fgtGate = 1.f / (1.f + expf(-intern[intern_offset + 2 * n_cells]));
          float outGate = 1.f / (1.f + expf(-intern[intern_offset + 3 * n_cells]));

          float c_b = prev_c_b * fgtGate + cellIn * inpGate;
          float gc = tanhf(c_b);
          float d_outGate_in = (1.f - outGate) * outGate * gc * d_y_b;
          float d_c2 = d_c_b + outGate * d_y_b * (1.f - gc * gc);
          float d_cellIn_in = (1.f - cellIn * cellIn) * inpGate * d_c2;
          float d_inpGate_in = (1.f - inpGate) * inpGate * cellIn * d_c2;
          float d_fgtGate_in = (1.f - fgtGate) * fgtGate * prev_c_b * d_c2;
          d_c[idx] = fgtGate * d_c2 + d_c[idx] * (1.f - mask_b);

          d_intern[intern_offset] = d_cellIn_in;
          d_intern[intern_offset + n_cells] = d_inpGate_in;
          d_intern[intern_offset + 2 * n_cells] = d_fgtGate_in;
          d_intern[intern_offset + 3 * n_cells] = d_outGate_in;

          elem_atomic_add(&d_b[cell_idx], d_cellIn_in);
          elem_atomic_add(&d_b[cell_idx + n_cells], d_inpGate_in);
          elem_atomic_add(&d_b[cell_idx + 2 * n_cells], d_fgtGate_in);
          elem_atomic_add(&d_b[cell_idx + 3 * n_cells], d_outGate_in);

          idx += gridDim.x * blockDim.x;
        }
      }
      """,
    "add_bias_kernel": """
      DEF_KERNEL
      void add_bias_kernel(int n_batch, int n_dim, float* x, float* b) {
        int idx = threadIdx.x + blockDim.x * blockIdx.x;
        while (idx < n_batch * n_dim) {
          int dim_idx = idx % n_dim;
          x[idx] += b[dim_idx];
          idx += gridDim.x * blockDim.x;
        }
      }
    """,
    "copy_x_h_kernel": """
      DEF_KERNEL
      void copy_x_h_kernel(
        int n_batch, int n_in, int n_cells,
        float* x_h,
        float* x,
        float* h)
      {
        int n_total_in = n_in + n_cells;
        int idx = threadIdx.x + blockDim.x * blockIdx.x;
        while (idx < n_batch * n_total_in) {
          int batch_idx = idx / n_total_in;
          int in_dim_idx = idx % n_total_in;

          if(in_dim_idx < n_in)
            x_h[idx] = x[batch_idx * n_in + in_dim_idx];
          else
            x_h[idx] = h[batch_idx * n_cells + in_dim_idx - n_in];

          idx += gridDim.x * blockDim.x;
        }
      }
      """,
    "inv_copy_x_h_kernel": """
    DEF_KERNEL
    void inv_copy_x_h_kernel(
      int n_batch, int n_in, int n_cells,
      float* x_h,
      float* x,
      float* h)
    {
      int n_total_in = n_in + n_cells;
      int idx = threadIdx.x + blockDim.x * blockIdx.x;
      while (idx < n_batch * n_total_in) {
        int batch_idx = idx / n_total_in;
        int in_dim_idx = idx % n_total_in;

        if(in_dim_idx < n_in)
          x[batch_idx * n_in + in_dim_idx] = x_h[idx];
        else
          h[batch_idx * n_cells + in_dim_idx - n_in] = x_h[idx];

        idx += gridDim.x * blockDim.x;
      }
    }
    """
  }

  c_fw_code = """
    // X, W, b, y0, c0, i, start, step = input_names
    // Y, C, d = output_names
    assert(n_inputs == 8);
    assert(n_outputs == 3);
    Ndarray* X = inputs[0];
    Ndarray* W = inputs[1];
    Ndarray* b = inputs[2];
    Ndarray* y0 = inputs[3];
    Ndarray* c0 = inputs[4];
    Ndarray* i = inputs[5];
    assert_cmp(Ndarray_NDIM(inputs[6]), ==, 0);
    assert_cmp(Ndarray_NDIM(inputs[7]), ==, 0);
    int start = Ndarray_DEV_DATA_int32_scalar(inputs[6]);
    int step = Ndarray_DEV_DATA_int32_scalar(inputs[7]);
    Ndarray* Y = *outputs[0];
    Ndarray* C = *outputs[1];
    Ndarray* d = *outputs[2];

    assert_cmp(Ndarray_NDIM(X), ==, 3);
    assert_cmp(Ndarray_NDIM(W), ==, 2);
    assert_cmp(Ndarray_NDIM(b), ==, 1);
    assert_cmp(Ndarray_NDIM(y0), ==, 2);
    assert_cmp(Ndarray_NDIM(c0), ==, 2);
    assert_cmp(Ndarray_NDIM(i), ==, 2);
    assert_cmp(Ndarray_NDIM(Y), ==, 3);
    assert_cmp(Ndarray_NDIM(C), ==, 3);
    assert_cmp(Ndarray_NDIM(d), ==, 2);
    long T = Ndarray_DIMS(i)[0];
    int n_batch = Ndarray_DIMS(i)[1];
    int n_cells = Ndarray_DIMS(y0)[1];
    int n_in = Ndarray_DIMS(X)[2];
    assert_cmp(Ndarray_DIMS(X)[0], ==, T);
    assert_cmp(Ndarray_DIMS(X)[1], ==, n_batch);
    assert_cmp(Ndarray_DIMS(W)[0], ==, n_in + n_cells);
    assert_cmp(Ndarray_DIMS(W)[1], ==, n_cells * 4);
    assert_cmp(Ndarray_DIMS(b)[0], ==, n_cells * 4);
    assert_cmp(Ndarray_DIMS(y0)[0], ==, n_batch);
    assert_cmp(Ndarray_DIMS(y0)[1], ==, n_cells);
    assert_cmp(Ndarray_DIMS(c0)[0], ==, n_batch);
    assert_cmp(Ndarray_DIMS(c0)[1], ==, n_cells);
    assert_cmp(Ndarray_DIMS(Y)[0], ==, T);
    assert_cmp(Ndarray_DIMS(Y)[1], ==, n_batch);
    assert_cmp(Ndarray_DIMS(Y)[2], ==, n_cells);
    assert_cmp(Ndarray_DIMS(C)[0], ==, T);
    assert_cmp(Ndarray_DIMS(C)[1], ==, n_batch);
    assert_cmp(Ndarray_DIMS(C)[2], ==, n_cells);
    assert_cmp(Ndarray_DIMS(d)[0], ==, n_batch);
    assert_cmp(Ndarray_DIMS(d)[1], ==, n_cells);

    float* x_h = (float*) device_malloc(n_batch * (n_in + n_cells) * sizeof(float));
    float* intern = (float*) device_malloc(n_batch * n_cells * 4 * sizeof(float));  // 3 gates + in

    assert_cmp(T, >, 0);
    assert_cmp(start, >=, 0);
    assert_cmp(start, <, T);
    assert_cmp(step, !=, 0);
    int end = T - 1;
    if(step < 0) {
      end = start;
      start = T - start - 1;
    }
    int t = start;
    for(; (step > 0) ? (t <= end) : (t >= end); t += step) {
      // x_h = X[t], Y[t-1]
      start_dev_kernel(copy_x_h_kernel,
        (n_batch, n_in, n_cells, x_h, data_ptr(X, t), (t != start) ? data_ptr(Y, t-step) : Ndarray_DEV_DATA(y0)));
      // intern = x_h * W
      affine_raw(
        x_h, n_batch, n_in + n_cells,
        Ndarray_DEV_DATA(W), n_in + n_cells, n_cells * 4,
        intern, n_batch, n_cells * 4,
        false, false, 0.0);
      // intern += b
      start_dev_kernel(add_bias_kernel, (
        n_batch, n_cells * 4, intern, Ndarray_DEV_DATA(b)));

      start_dev_kernel(lstm_kernel, (
        n_batch,
        n_cells,
        Ndarray_DEV_DATA(i) + t * n_batch,
        intern,
        (t != start) ? data_ptr(C, t-step) : Ndarray_DEV_DATA(c0),
        data_ptr(Y, t),  // out
        data_ptr(C, t)  // out
      ));
    }

    device_free(x_h);
    device_free(intern);

    Ndarray_memcpy(Ndarray_DEV_DATA(d), data_ptr(C, t - step), n_batch * n_cells * sizeof(float));
  """

  # language=C++
  c_bw_code = """
    // X, W, b, y0, c0, i, start, step,   Y, C,   DY, Dd = input_names
    // DX, DW, Db, Dh, Dc = output_names
    assert(n_inputs == 12);
    assert(n_outputs == 5);
    Ndarray* X = inputs[0];
    Ndarray* W = inputs[1];
    Ndarray* b = inputs[2];
    Ndarray* y0 = inputs[3];
    Ndarray* c0 = inputs[4];
    Ndarray* i = inputs[5];
    assert_cmp(Ndarray_NDIM(inputs[6]), ==, 0);
    assert_cmp(Ndarray_NDIM(inputs[7]), ==, 0);
    int start = Ndarray_DEV_DATA_int32_scalar(inputs[6]);
    int step = Ndarray_DEV_DATA_int32_scalar(inputs[7]);
    Ndarray* Y = inputs[8];
    Ndarray* C = inputs[9];
    Ndarray* DY = inputs[10];
    Ndarray* Dd = inputs[11];
    Ndarray* DX = *outputs[0];
    Ndarray* DW = *outputs[1];
    Ndarray* Db = *outputs[2];
    Ndarray* Dh = *outputs[3];
    Ndarray* Dc = *outputs[4];

    assert_cmp(Ndarray_NDIM(X), ==, 3);
    assert_cmp(Ndarray_NDIM(W), ==, 2);
    assert_cmp(Ndarray_NDIM(b), ==, 1);
    assert_cmp(Ndarray_NDIM(y0), ==, 2);
    assert_cmp(Ndarray_NDIM(c0), ==, 2);
    assert_cmp(Ndarray_NDIM(i), ==, 2);
    assert_cmp(Ndarray_NDIM(Y), ==, 3);
    assert_cmp(Ndarray_NDIM(C), ==, 3);
    assert_cmp(Ndarray_NDIM(DY), ==, 3);
    assert_cmp(Ndarray_NDIM(Dd), ==, 2);
    assert_cmp(Ndarray_NDIM(DX), ==, 3);
    assert_cmp(Ndarray_NDIM(DW), ==, 2);
    assert_cmp(Ndarray_NDIM(Db), ==, 1);
    assert_cmp(Ndarray_NDIM(Dh), ==, 2);
    assert_cmp(Ndarray_NDIM(Dc), ==, 2);
    long T = Ndarray_DIMS(i)[0];
    int n_batch = Ndarray_DIMS(i)[1];
    int n_cells = Ndarray_DIMS(y0)[1];
    int n_in = Ndarray_DIMS(X)[2];
    assert_cmp(Ndarray_DIMS(X)[0], ==, T);
    assert_cmp(Ndarray_DIMS(X)[1], ==, n_batch);
    assert_cmp(Ndarray_DIMS(W)[0], ==, n_in + n_cells);
    assert_cmp(Ndarray_DIMS(W)[1], ==, n_cells * 4);
    assert_cmp(Ndarray_DIMS(b)[0], ==, n_cells * 4);
    assert_cmp(Ndarray_DIMS(y0)[0], ==, n_batch);
    assert_cmp(Ndarray_DIMS(y0)[1], ==, n_cells);
    assert_cmp(Ndarray_DIMS(c0)[0], ==, n_batch);
    assert_cmp(Ndarray_DIMS(c0)[1], ==, n_cells);
    assert_cmp(Ndarray_DIMS(Y)[0], ==, T);
    assert_cmp(Ndarray_DIMS(Y)[1], ==, n_batch);
    assert_cmp(Ndarray_DIMS(Y)[2], ==, n_cells);
    assert_cmp(Ndarray_DIMS(C)[0], ==, T);
    assert_cmp(Ndarray_DIMS(C)[1], ==, n_batch);
    assert_cmp(Ndarray_DIMS(C)[2], ==, n_cells);
    assert_cmp(Ndarray_DIMS(DY)[0], ==, T);
    assert_cmp(Ndarray_DIMS(DY)[1], ==, n_batch);
    assert_cmp(Ndarray_DIMS(DY)[2], ==, n_cells);
    assert_cmp(Ndarray_DIMS(Dd)[0], ==, n_batch);
    assert_cmp(Ndarray_DIMS(Dd)[1], ==, n_cells);
    assert_cmp(Ndarray_DIMS(DX)[0], ==, T);
    assert_cmp(Ndarray_DIMS(DX)[1], ==, n_batch);
    assert_cmp(Ndarray_DIMS(DX)[2], ==, n_in);
    assert_cmp(Ndarray_DIMS(DW)[0], ==, n_in + n_cells);
    assert_cmp(Ndarray_DIMS(DW)[1], ==, n_cells * 4);
    assert_cmp(Ndarray_DIMS(Db)[0], ==, n_cells * 4);
    assert_cmp(Ndarray_DIMS(Dh)[0], ==, n_batch);
    assert_cmp(Ndarray_DIMS(Dh)[1], ==, n_cells);
    assert_cmp(Ndarray_DIMS(Dc)[0], ==, n_batch);
    assert_cmp(Ndarray_DIMS(Dc)[1], ==, n_cells);

    float* x_h = (float*) device_malloc(n_batch * (n_in + n_cells) * sizeof(float));
    float* intern = (float*) device_malloc(n_batch * n_cells * 4 * sizeof(float));  // 3 gates + in
    float* Dx_h = (float*) device_malloc(n_batch * (n_in + n_cells) * sizeof(float));
    float* Dintern = (float*) device_malloc(n_batch * n_cells * 4 * sizeof(float));  // 3 gates + in

    // We will work inplace on DX/DW/Db.
    Ndarray_memset(Ndarray_DEV_DATA(DX), 0, T * n_batch * n_in * sizeof(float));
    Ndarray_memset(Ndarray_DEV_DATA(DW), 0, (n_in + n_cells) * n_cells * 4 * sizeof(float));
    Ndarray_memset(Ndarray_DEV_DATA(Db), 0, n_cells * 4 * sizeof(float));
    // We will work inplace on Dh.
    Ndarray_memset(Ndarray_DEV_DATA(Dh), 0, n_batch * n_cells * sizeof(float));
    // We will work inplace on Dc, and init it with Dd.
    Ndarray_memcpy(Ndarray_DEV_DATA(Dc), Ndarray_DEV_DATA(Dd), n_batch * n_cells * sizeof(float));

    assert_cmp(T, >, 0);
    assert_cmp(start, >=, 0);
    assert_cmp(start, <, T);
    assert_cmp(step, !=, 0);
    int end = T - 1;
    if(step < 0) {
      end = start;
      start = T - start - 1;
    }
    int t = end;  // go backwards
    for(; (step > 0) ? (t >= start) : (t <= start); t -= step) {
      bool right = (step > 0) ? (t - step >= start) : (t - step <= start);

      // TODO: correct handling of mask in grad, fwd, initial cell,hidden, etc
      // x_h = X[t], Y[t-1]
      start_dev_kernel(copy_x_h_kernel,
        (n_batch, n_in, n_cells,
         x_h, data_ptr(X, t), right ? data_ptr(Y, t-step) : Ndarray_DEV_DATA(y0)));

      // intern = x_h * W
      affine_raw(
        x_h, n_batch, n_in + n_cells,
        Ndarray_DEV_DATA(W), n_in + n_cells, n_cells * 4,
        intern, n_batch, n_cells * 4,
        false, false, 0.0);
      // intern += b
      start_dev_kernel(add_bias_kernel, (
        n_batch, n_cells * 4, intern, Ndarray_DEV_DATA(b)));

      start_dev_kernel(lstm_bwd_kernel, (
        n_batch,
        n_in,
        n_cells,
        Ndarray_DEV_DATA(i) + t * n_batch,
        x_h,
        intern,
        right ? data_ptr(C, t-step) : Ndarray_DEV_DATA(c0),
        data_ptr(Y, t),
        data_ptr(C, t),
        data_ptr(DY, t),
        Ndarray_DEV_DATA(Dh),  // error from prev frame, excluding DY. updated below
        Ndarray_DEV_DATA(Dc),  // in+out, working inplace. also error from prev frame, initially Dd
        Dintern,  // out
        Ndarray_DEV_DATA(Db)  // out
      ));

      // Dx_h = Dintern * W^T
      affine_raw(
        Dintern, n_batch, n_cells * 4,
        Ndarray_DEV_DATA(W), n_in + n_cells, n_cells * 4,
        Dx_h, n_batch, n_in + n_cells,
        false, true, 0.0);

      // DW += x_h^T * Dintern
      affine_raw(
        x_h, n_batch, n_in + n_cells,
        Dintern, n_batch, n_cells * 4,
        Ndarray_DEV_DATA(DW), n_in + n_cells, n_cells * 4,
        true, false);

      // DX[t], Dh = Dx_h
      start_dev_kernel(inv_copy_x_h_kernel,
        (n_batch, n_in, n_cells, Dx_h, data_ptr(DX, t), Ndarray_DEV_DATA(Dh)));
    }

    device_free(x_h);
    device_free(intern);
    device_free(Dx_h);
    device_free(Dintern);
  """


class Chunking(NativeOpGenBase):
  """
  Given an input in 3d (n_time,n_batch,n_dim), we chunk up the time dimension
  in chunks of size chunk_size, every chunk_step frames.
  This results in an 3d output (chunk_size, n_batch * n_chunks, n_dim)
  where n_chunks = floor( max(n_time - chunk_size + chunk_step - 1, 0) / chunk_step ) + 1.
  Examples:
    n_time=1,   chunk_size=50, chunk_step=10 -> n_chunks=1
    n_time=49,  chunk_size=50, chunk_step=10 -> n_chunks=1
    n_time=50,  chunk_size=50, chunk_step=10 -> n_chunks=1
    n_time=51,  chunk_size=50, chunk_step=10 -> n_chunks=2
    n_time=60,  chunk_size=50, chunk_step=10 -> n_chunks=2
    n_time=61,  chunk_size=50, chunk_step=10 -> n_chunks=3
    n_time=99,  chunk_size=50, chunk_step=10 -> n_chunks=6
    n_time=100, chunk_size=50, chunk_step=10 -> n_chunks=6
    n_time=101, chunk_size=50, chunk_step=10 -> n_chunks=7
  """
  in_info = (
    {"name": "input", "ndim": 3, "shape": (None, None, None)},
    {"name": "index", "ndim": 2, "shape": (None, None), "gradient": "disconnected"},
    {"name": "output_buffer", "ndim": 3, "shape": (None, None, None), "want_inplace": 0, "gradient": "disconnected"},
    {"name": "oindex_buffer", "ndim": 2, "shape": (None, None), "want_inplace": 1, "gradient": "disconnected"},
    {"name": "chunk_params", "ndim": 1, "shape": (2,), "need_contiguous": True, "gradient": "disconnected"},  # (chunk_size, chunk_step)
  )
  out_info = (
    {"name": "output", "ndim": 3, "shape": ((2, 0), (2, 1), (2, 2))},
    {"name": "oindex", "ndim": 2, "shape": ((3, 0), (3, 1))}
  )

  c_extra_support_code = {
    "copy_kernel": """
    DEF_KERNEL
    void copy_kernel(
      float* chunk_params,
      float* input, long in_dim0, long in_dim1, long in_dim2, long in_stride0, long in_stride1, long in_stride2,
      float* index, long idx_stride0, long idx_stride1,
      float* output, long out_dim0, long out_dim1, long out_stride0, long out_stride1, long out_stride2,
      float* oindex, long oidx_stride0, long oidx_stride1
    ) {
      assert_cmp(out_dim1 % in_dim1, ==, 0);
      const long n_chunks = out_dim1 / in_dim1;
      assert_cmp(n_chunks, >, 0);
      const long chunk_size = out_dim0;
      assert_cmp(long(chunk_params[0]), ==, chunk_size);
      const long chunk_step = long(chunk_params[1]);
      assert_cmp(chunk_step, >, 0);
      assert_cmp(chunk_step * (n_chunks - 1) + chunk_size, >=, in_dim0);
      assert_cmp(chunk_step * (n_chunks - 1), <, in_dim0);

      // Iterate over output (chunked) x/y coordinates.
      // In an inner loop, we will loop over z.
      const long max_idx = out_dim0 * out_dim1;
      for(
        long idx = threadIdx.x + blockDim.x * blockIdx.x;
        idx < max_idx;
        idx += gridDim.x * blockDim.x)
      {
        long out_x = idx % out_dim0;  // time
        long out_y = idx / out_dim0;  // batch

        long chunk_idx = out_y % n_chunks;
        long in_y =      out_y / n_chunks;

        long in_x = chunk_step * chunk_idx + out_x;

        if(in_x < in_dim0 && index[in_x * idx_stride0 + in_y * idx_stride1] > 0.1) {
          for(long z = 0; z < in_dim2; ++z)
            output[out_x * out_stride0 + out_y * out_stride1 + z * out_stride2] =
              input[in_x * in_stride0 + in_y * in_stride1 + z * in_stride2];
          oindex[out_x * oidx_stride0 + out_y * oidx_stride1] = 1;
        }
        else {
          for(long z = 0; z < in_dim2; ++z)
            output[out_x * out_stride0 + out_y * out_stride1 + z * out_stride2] = 0;
          oindex[out_x * oidx_stride0 + out_y * oidx_stride1] = 0;
        }
      }
    }
    """
  }

  c_fw_code = """
    assert_cmp(n_inputs, ==, 5);
    assert_cmp(n_outputs, ==, 2);
    Ndarray* input = inputs[0];
    Ndarray* index = inputs[1];
    Ndarray* chunk_params = inputs[4];
    Ndarray* output = *outputs[0];
    Ndarray* oindex = *outputs[1];

    assert_cmp(Ndarray_NDIM(input), ==, 3);
    assert_cmp(Ndarray_NDIM(index), ==, 2);
    assert_cmp(Ndarray_DIMS(input)[0], ==, Ndarray_DIMS(index)[0]);
    assert_cmp(Ndarray_DIMS(input)[1], ==, Ndarray_DIMS(index)[1]);
    assert_cmp(Ndarray_NDIM(chunk_params), ==, 1);
    assert_cmp(Ndarray_DIMS(chunk_params)[0], ==, 2);
    assert_cmp(Ndarray_NDIM(output), ==, 3);
    assert_cmp(Ndarray_NDIM(oindex), ==, 2);
    assert_cmp(Ndarray_DIMS(output)[0], ==, Ndarray_DIMS(oindex)[0]);
    assert_cmp(Ndarray_DIMS(output)[1], ==, Ndarray_DIMS(oindex)[1]);
    assert_cmp(Ndarray_DIMS(output)[2], ==, Ndarray_DIMS(input)[2]);

    start_dev_kernel(copy_kernel, (
      Ndarray_DEV_DATA(chunk_params),
      Ndarray_DEV_DATA(input),
        Ndarray_DIMS(input)[0],
        Ndarray_DIMS(input)[1],
        Ndarray_DIMS(input)[2],
        Ndarray_STRIDE(input, 0),
        Ndarray_STRIDE(input, 1),
        Ndarray_STRIDE(input, 2),
      Ndarray_DEV_DATA(index),
        Ndarray_STRIDE(index, 0),
        Ndarray_STRIDE(index, 1),
      Ndarray_DEV_DATA(output),
        Ndarray_DIMS(output)[0],
        Ndarray_DIMS(output)[1],
        Ndarray_STRIDE(output, 0),
        Ndarray_STRIDE(output, 1),
        Ndarray_STRIDE(output, 2),
      Ndarray_DEV_DATA(oindex),
        Ndarray_STRIDE(oindex, 0),
        Ndarray_STRIDE(oindex, 1)
    ));
  """

  code_version = ()

  @staticmethod
  def naive_chunk_start_frames(n_time, chunk_size, chunk_step):
    """
    This is just for documentation / demonstration. Also used by testing code.
    """
    t = 0
    chunk_start_frames = []
    while True:
      chunk_start_frames.append(t)
      if t + chunk_size >= n_time: break
      t += chunk_step
    return chunk_start_frames

  @classmethod
  def custom_grad(cls, op, inputs, output_grads):
    assert len(op.in_info) == len(inputs)
    assert len(op.out_info) == len(output_grads)

    input, index, _, _, chunk_params = inputs
    Dout, _ = output_grads

    assert input.ndim == 3
    n_time = input.shape[0]
    n_batch = input.shape[1]
    chunk_size = chunk_params[0]
    chunk_step = chunk_params[1]
    out, oindex = op(*inputs)
    Dinput, _, factors = unchunk(Dout, index=oindex, chunk_size=chunk_size, chunk_step=chunk_step, n_time=n_time, n_batch=n_batch)
    # We applied the factor in unchunk, but for this gradient, we actually don't want that, so undo it.
    Dinput /= factors.dimshuffle(0, 1, 'x')

    grads = [Dinput] + [T.DisconnectedType()() for inp in inputs[1:]]
    assert len(grads) == len(inputs)
    return grads


def chunk(x, index, chunk_size, chunk_step):
  assert x.ndim == 3
  n_time = x.shape[0]
  n_batch = x.shape[1]
  n_dim = x.shape[2]
  if isinstance(chunk_size, T.TensorVariable):
    chunk_size = T.cast(chunk_size, "int64")
  if isinstance(chunk_step, T.TensorVariable):
    chunk_step = T.cast(chunk_step, "int64")
  n_chunks = T.maximum(n_time - chunk_size + chunk_step - 1, 0) // chunk_step + 1
  chunk_params = T.concatenate([T.as_tensor(chunk_size).reshape((1,)), T.as_tensor(chunk_step).reshape((1,))])
  out_buffer = T.zeros((chunk_size, n_batch * n_chunks, n_dim), dtype=x.dtype)
  oindex_buffer = T.zeros((chunk_size, n_batch * n_chunks), dtype=index.dtype)
  chunk_op = Chunking().make_op()
  out, oindex = chunk_op(x, index, out_buffer, oindex_buffer, chunk_params)
  return out, oindex


class UnChunking(NativeOpGenBase):
  """
  This reverses the output from `Chunking`, i.e. chunking the time dimension.
  We get a 3d input (chunk_size, n_batch * n_chunks, n_dim)
  and return an 3d output (n_time, n_batch, n_dim)
  where the chunks are of size chunk_size, every chunk_step frames.
  Because of overlaps, we have to combine the overlapping chunks somehow.
  We will do that with a uniform distribution, i.e. take the mean of all overlaps per frame.
  """
  in_info = (
    {"name": "input", "ndim": 3, "shape": (None, None, None)},
    {"name": "index", "ndim": 2, "shape": (None, None), "gradient": "disconnected"},
    {"name": "output_buffer", "ndim": 3, "shape": (None, None, None), "want_inplace": 0, "gradient": "disconnected"},
    {"name": "oindex_buffer", "ndim": 2, "shape": (None, None), "want_inplace": 1, "gradient": "disconnected"},
    {"name": "ofactors_buffer", "ndim": 2, "shape": (None, None), "want_inplace": 2, "gradient": "disconnected"},
    {"name": "chunk_params", "ndim": 1, "shape": (2,), "need_contiguous": True, "gradient": "disconnected"},  # (chunk_size, chunk_step)
  )
  out_info = (
    {"name": "output", "ndim": 3, "shape": ((2, 0), (2, 1), (2, 2))},
    {"name": "oindex", "ndim": 2, "shape": ((3, 0), (3, 1))},
    {"name": "ofactors", "ndim": 2, "shape": ((4, 0), (4, 1))}
  )

  c_extra_support_code = {
    "unchunk_kernel": """
    DEF_KERNEL
    void unchunk_kernel(
      float* chunk_params,
      float* input, long in_dim0, long in_dim1, long in_dim2, long in_stride0, long in_stride1, long in_stride2,
      float* index, long idx_stride0, long idx_stride1,
      float* output, long out_dim0, long out_dim1, long out_stride0, long out_stride1, long out_stride2,
      float* oindex, long oidx_stride0, long oidx_stride1,
      float* ofactors, long ofac_stride0, long ofac_stride1
    ) {
      assert_cmp(in_dim1 % out_dim1, ==, 0);
      const long n_chunks = in_dim1 / out_dim1;
      assert_cmp(n_chunks, >, 0);
      const long chunk_size = in_dim0;
      assert_cmp(long(chunk_params[0]), ==, chunk_size);
      const long chunk_step = long(chunk_params[1]);
      assert_cmp(chunk_step, >, 0);
      assert_cmp(chunk_step * (n_chunks - 1) + chunk_size, >=, out_dim0);
      assert_cmp(chunk_step * (n_chunks - 1), <, out_dim0);

      // Iterate over output (unchunked) x/y coordinates.
      // In an inner loop, we will loop over z.
      const long max_idx = out_dim0 * out_dim1;
      for(
        long idx = threadIdx.x + blockDim.x * blockIdx.x;
        idx < max_idx;
        idx += gridDim.x * blockDim.x)
      {
        long out_x = idx % out_dim0;  // time
        long out_y = idx / out_dim0;  // batch

        float c = 0;
        for(long z = 0; z < in_dim2; ++z)
          output[out_x * out_stride0 + out_y * out_stride1 + z * out_stride2] = 0;

        // in_x = out_x - chunk_step * chunk_idx,
        // thus in_x < 0           when chunk_idx * chunk_step >  out_x,
        // and  in_x >= chunk_size when chunk_idx * chunk_step <= out_x - chunk_size,
        // thus we need chunk_idx <= out_x / chunk_step,
        // and          chunk_idx > (out_x - chunk_size) / chunk_step.
        // Examples:
        //   out_x=0,  chunk_size=10, chunk_step=4 -> chunk_idx_start,end=0,1
        //   out_x=3,  chunk_size=10, chunk_step=4 -> chunk_idx_start,end=0,1
        //   out_x=4,  chunk_size=10, chunk_step=4 -> chunk_idx_start,end=0,2
        //   out_x=7,  chunk_size=10, chunk_step=4 -> chunk_idx_start,end=0,2
        //   out_x=8,  chunk_size=10, chunk_step=4 -> chunk_idx_start,end=0,3
        //   out_x=9,  chunk_size=10, chunk_step=4 -> chunk_idx_start,end=0,3
        //   out_x=10, chunk_size=10, chunk_step=4 -> chunk_idx_start,end=1,3
        //   out_x=11, chunk_size=10, chunk_step=4 -> chunk_idx_start,end=1,3
        //   out_x=12, chunk_size=10, chunk_step=4 -> chunk_idx_start,end=1,4
        //   out_x=13, chunk_size=10, chunk_step=4 -> chunk_idx_start,end=1,4
        //   out_x=14, chunk_size=10, chunk_step=4 -> chunk_idx_start,end=2,4
        long chunk_idx_start = (out_x - chunk_size + chunk_step) / chunk_step;
        if(chunk_idx_start < 0) chunk_idx_start = 0;
        long chunk_idx_end = out_x / chunk_step + 1;
        if(chunk_idx_end > n_chunks) chunk_idx_end = n_chunks;
        assert_cmp(chunk_idx_start, <, chunk_idx_end);
        for(long chunk_idx = chunk_idx_start; chunk_idx < chunk_idx_end; ++chunk_idx) {
          long in_y = out_y * n_chunks + chunk_idx;
          long in_x = out_x - chunk_step * chunk_idx;
          assert_cmp(in_x, >=, 0);
          assert_cmp(in_x, <, chunk_size);
          if(index[in_x * idx_stride0 + in_y * idx_stride1] > 0.1) {
            c += 1;
            for(long z = 0; z < in_dim2; ++z)
              output[out_x * out_stride0 + out_y * out_stride1 + z * out_stride2] +=
                input[in_x * in_stride0 + in_y * in_stride1 + z * in_stride2];
          }
        }

        if(c > 0.1) {
          for(long z = 0; z < in_dim2; ++z)
            output[out_x * out_stride0 + out_y * out_stride1 + z * out_stride2] /= c;
          oindex[out_x * oidx_stride0 + out_y * oidx_stride1] = 1;
          ofactors[out_x * ofac_stride0 + out_y * ofac_stride1] = 1.0 / c;
        } else {
          oindex[out_x * oidx_stride0 + out_y * oidx_stride1] = 0;
          ofactors[out_x * ofac_stride0 + out_y * ofac_stride1] = 1.0;
        }
      }
    }
    """
  }

  c_fw_code = """
    assert_cmp(n_inputs, ==, 6);
    assert_cmp(n_outputs, ==, 3);
    Ndarray* input = inputs[0];
    Ndarray* index = inputs[1];
    Ndarray* chunk_params = inputs[5];
    Ndarray* output = *outputs[0];
    Ndarray* oindex = *outputs[1];
    Ndarray* ofactors = *outputs[2];

    assert_cmp(Ndarray_NDIM(input), ==, 3);
    assert_cmp(Ndarray_NDIM(index), ==, 2);
    assert_cmp(Ndarray_DIMS(input)[0], ==, Ndarray_DIMS(index)[0]);
    assert_cmp(Ndarray_DIMS(input)[1], ==, Ndarray_DIMS(index)[1]);
    assert_cmp(Ndarray_NDIM(chunk_params), ==, 1);
    assert_cmp(Ndarray_DIMS(chunk_params)[0], ==, 2);
    assert_cmp(Ndarray_NDIM(output), ==, 3);
    assert_cmp(Ndarray_NDIM(oindex), ==, 2);
    assert_cmp(Ndarray_NDIM(ofactors), ==, 2);
    assert_cmp(Ndarray_DIMS(output)[0], ==, Ndarray_DIMS(oindex)[0]);
    assert_cmp(Ndarray_DIMS(output)[1], ==, Ndarray_DIMS(oindex)[1]);
    assert_cmp(Ndarray_DIMS(output)[2], ==, Ndarray_DIMS(input)[2]);
    assert_cmp(Ndarray_DIMS(oindex)[0], ==, Ndarray_DIMS(ofactors)[0]);
    assert_cmp(Ndarray_DIMS(oindex)[1], ==, Ndarray_DIMS(ofactors)[1]);

    start_dev_kernel(unchunk_kernel, (
      Ndarray_DEV_DATA(chunk_params),
      Ndarray_DEV_DATA(input),
        Ndarray_DIMS(input)[0],
        Ndarray_DIMS(input)[1],
        Ndarray_DIMS(input)[2],
        Ndarray_STRIDE(input, 0),
        Ndarray_STRIDE(input, 1),
        Ndarray_STRIDE(input, 2),
      Ndarray_DEV_DATA(index),
        Ndarray_STRIDE(index, 0),
        Ndarray_STRIDE(index, 1),
      Ndarray_DEV_DATA(output),
        Ndarray_DIMS(output)[0],
        Ndarray_DIMS(output)[1],
        Ndarray_STRIDE(output, 0),
        Ndarray_STRIDE(output, 1),
        Ndarray_STRIDE(output, 2),
      Ndarray_DEV_DATA(oindex),
        Ndarray_STRIDE(oindex, 0),
        Ndarray_STRIDE(oindex, 1),
      Ndarray_DEV_DATA(ofactors),
        Ndarray_STRIDE(ofactors, 0),
        Ndarray_STRIDE(ofactors, 1)
    ));
  """

  code_version = ()

  @classmethod
  def custom_grad(cls, op, inputs, output_grads):
    assert len(op.in_info) == len(inputs)
    assert len(op.out_info) == len(output_grads)

    input, index, _, _, _, chunk_params = inputs
    Dout, _, _ = output_grads

    chunk_size = chunk_params[0]
    chunk_step = chunk_params[1]
    out, oindex, factors = op(*inputs)
    Dout *= factors.dimshuffle(0, 1, 'x')
    Dinput, _ = chunk(Dout, index=oindex, chunk_size=chunk_size, chunk_step=chunk_step)

    grads = [Dinput] + [T.DisconnectedType()() for inp in inputs[1:]]
    assert len(grads) == len(inputs)
    return grads


def unchunk(x, index, chunk_size, chunk_step, n_time, n_batch):
  assert x.ndim == 3
  n_dim = x.shape[2]
  chunk_params = T.concatenate([T.as_tensor(chunk_size).reshape((1,)), T.as_tensor(chunk_step).reshape((1,))])
  out_buffer = T.zeros((n_time, n_batch, n_dim), dtype=x.dtype)
  oindex_buffer = T.zeros((n_time, n_batch), dtype=index.dtype)
  ofactors_buffer = T.zeros((n_time, n_batch), dtype=x.dtype)
  unchunk_op = UnChunking().make_op()
  out, oindex, ofactors = unchunk_op(x, index, out_buffer, oindex_buffer, ofactors_buffer, chunk_params)
  return out, oindex, ofactors


class SubtensorBatchedIndex(NativeOpGenBase):
  """
  Consider you have:
    idx: 2d (n_time, n_batch) -> idx (in [0..n_dim-1])
    x: 3d (n_time, n_batch, n_dim)
  Then, this op will calculate:
    x[..., idx[...]]: 2d (n_time, n_batch)
  """
  in_info = (
    {"name": "x", "ndim": 3, "shape": (None, None, None), "bw_in_var": {"want_inplace": 0}},
    {"name": "idx", "ndim": 2, "shape": (None, None), "gradient": "disconnected"}
  )
  out_info = (
    {"name": "y", "ndim": 2, "shape": ((0, 0), (0, 1))},
  )
  @classmethod
  def grad_input_map(cls, x, idx,  y,  DY):
    return (x, idx, DY)

  c_extra_support_code = {
    "select_kernel": """
    DEF_KERNEL
    void select_kernel(
      float* x, long x_dim0, long x_dim1, long x_dim2, long x_stride0, long x_stride1, long x_stride2,
      float* index, long idx_stride0, long idx_stride1,
      float* y, long y_stride0, long y_stride1
    ) {
      const long max_idx = x_dim0 * x_dim1;
      for(
        long idx = threadIdx.x + blockDim.x * blockIdx.x;
        idx < max_idx;
        idx += gridDim.x * blockDim.x)
      {
        long d0 = idx % x_dim0;
        long d1 = idx / x_dim0;
        long d2 = long(index[d0 * idx_stride0 + d1 * idx_stride1]);
        if(d2 < 0) d2 = 0;
        if(d2 >= x_dim2) d2 = x_dim2 - 1;
        y[d0 * y_stride0 + d1 * y_stride1] = x[d0 * x_stride0 + d1 * x_stride1 + d2 * x_stride2];
      }
    }
    """,
    "select_bw_kernel": """
    DEF_KERNEL
    void select_bw_kernel(
      float* Dx, long Dx_dim0, long Dx_dim1, long Dx_dim2, long Dx_stride0, long Dx_stride1, long Dx_stride2,
      float* index, long idx_stride0, long idx_stride1,
      float* Dy, long Dy_stride0, long Dy_stride1
    ) {
      const long max_idx = Dx_dim0 * Dx_dim1;
      for(
        long idx = threadIdx.x + blockDim.x * blockIdx.x;
        idx < max_idx;
        idx += gridDim.x * blockDim.x)
      {
        long d0 = idx % Dx_dim0;
        long d1 = idx / Dx_dim0;
        long d2 = long(index[d0 * idx_stride0 + d1 * idx_stride1]);
        if(d2 < 0) d2 = 0;
        if(d2 >= Dx_dim2) d2 = Dx_dim2 - 1;
        Dx[d0 * Dx_stride0 + d1 * Dx_stride1 + d2 * Dx_stride2] = Dy[d0 * Dy_stride0 + d1 * Dy_stride1];
      }
    }
    """
  }

  c_fw_code = """
    assert_cmp(n_inputs, ==, 2);
    assert_cmp(n_outputs, ==, 1);
    Ndarray* x = inputs[0];
    Ndarray* idx = inputs[1];
    Ndarray* y = *outputs[0];

    assert_cmp(Ndarray_NDIM(x), ==, 3);
    assert_cmp(Ndarray_NDIM(idx), ==, 2);
    assert_cmp(Ndarray_DIMS(x)[0], ==, Ndarray_DIMS(idx)[0]);
    assert_cmp(Ndarray_DIMS(x)[1], ==, Ndarray_DIMS(idx)[1]);
    assert_cmp(Ndarray_NDIM(y), ==, 2);
    assert_cmp(Ndarray_DIMS(y)[0], ==, Ndarray_DIMS(idx)[0]);
    assert_cmp(Ndarray_DIMS(y)[1], ==, Ndarray_DIMS(idx)[1]);

    start_dev_kernel(select_kernel, (
      Ndarray_DEV_DATA(x),
        Ndarray_DIMS(x)[0],
        Ndarray_DIMS(x)[1],
        Ndarray_DIMS(x)[2],
        Ndarray_STRIDE(x, 0),
        Ndarray_STRIDE(x, 1),
        Ndarray_STRIDE(x, 2),
      Ndarray_DEV_DATA(idx),
        Ndarray_STRIDE(idx, 0),
        Ndarray_STRIDE(idx, 1),
      Ndarray_DEV_DATA(y),
        Ndarray_STRIDE(y, 0),
        Ndarray_STRIDE(y, 1)
    ));
  """

  c_bw_code = """
    assert_cmp(n_inputs, ==, 3);
    assert_cmp(n_outputs, ==, 1);
    Ndarray* x = inputs[0];
    Ndarray* idx = inputs[1];
    Ndarray* Dy = inputs[2];
    Ndarray* Dx = *outputs[0];  // inplace on x

    assert_cmp(Ndarray_NDIM(x), ==, 3);
    assert_cmp(Ndarray_NDIM(idx), ==, 2);
    assert_cmp(Ndarray_DIMS(x)[0], ==, Ndarray_DIMS(idx)[0]);
    assert_cmp(Ndarray_DIMS(x)[1], ==, Ndarray_DIMS(idx)[1]);
    assert_cmp(Ndarray_NDIM(Dy), ==, 2);
    assert_cmp(Ndarray_DIMS(Dy)[0], ==, Ndarray_DIMS(idx)[0]);
    assert_cmp(Ndarray_DIMS(Dy)[1], ==, Ndarray_DIMS(idx)[1]);
    assert_cmp(Ndarray_NDIM(Dx), ==, 3);
    assert_cmp(Ndarray_DIMS(Dx)[0], ==, Ndarray_DIMS(x)[0]);
    assert_cmp(Ndarray_DIMS(Dx)[1], ==, Ndarray_DIMS(x)[1]);
    assert_cmp(Ndarray_DIMS(Dx)[2], ==, Ndarray_DIMS(x)[2]);

    Ndarray_set_zero(Dx);
    start_dev_kernel(select_bw_kernel, (
      Ndarray_DEV_DATA(Dx),
        Ndarray_DIMS(Dx)[0],
        Ndarray_DIMS(Dx)[1],
        Ndarray_DIMS(Dx)[2],
        Ndarray_STRIDE(Dx, 0),
        Ndarray_STRIDE(Dx, 1),
        Ndarray_STRIDE(Dx, 2),
      Ndarray_DEV_DATA(idx),
        Ndarray_STRIDE(idx, 0),
        Ndarray_STRIDE(idx, 1),
      Ndarray_DEV_DATA(Dy),
        Ndarray_STRIDE(Dy, 0),
        Ndarray_STRIDE(Dy, 1)
    ));
  """


def subtensor_batched_index(x, idx):
  if x.ndim == 2:
    assert idx.ndim == 1
    x = x.reshape((x.shape[0], 1, x.shape[1]))
    idx = idx.reshape((idx.shape[0], 1))
    y = subtensor_batched_index(x, idx)
    return y[:, 0]
  assert x.ndim == 3
  assert idx.ndim == 2
  op = SubtensorBatchedIndex().make_op()
  return op(x, idx)


class SparseToDense(NativeOpGenBase):
  """
  Expects a sparse matrix in COOrdinate format,
  where W[s0[i,b],b,s1[i]] = weight[i,b] for all i, and all batches b.
  Will return W (time,batch,dim).
  """
  in_info = (
    {"name": "_initial_W", "ndim": 3, "shape": (None, None, None), "need_contiguous": True, "want_inplace": 0},
    {"name": "s0", "ndim": 2, "shape": (None, None), "need_contiguous": True},
    {"name": "s1", "ndim": 2, "shape": (None, None), "need_contiguous": True},
    {"name": "weight", "ndim": 2, "shape": (None, None), "need_contiguous": True},
    {"name": "mask", "ndim": 2, "shape": (None, None), "need_contiguous": True}
  )
  out_info = (
    {"name": "W", "ndim": 3, "shape": ((0, 0), (0, 1), (0, 2))},
  )

  c_extra_support_code = {
    "assign_kernel": """
    DEF_KERNEL
    void assign_kernel(
      float* out, float* s0, float* s1, float* w, float* mask,
      long n_sparse_idx, long n_time, long n_batch, long n_dim)
    {
      long max_idx = n_batch * n_sparse_idx;
      for(
        long idx = threadIdx.x + blockDim.x * blockIdx.x;
        idx < max_idx;
        idx += gridDim.x * blockDim.x)
      {
        if(mask[idx] < 0.1) continue;
        long batch = idx % n_batch;
        long t = (long) s0[idx];
        long j = (long) s1[idx];
        float y = w[idx];
        if(t < 0 || t >= n_time) continue;  // error somehow?
        if(j < 0 || j >= n_dim) continue;  // error somehow?
        long out_idx = t * n_batch * n_dim + batch * n_dim + j;
        out[out_idx] += y;
      }
    }
    """
  }

  c_fw_code = """
    assert(n_inputs == 5);
    assert(n_outputs == 1);
    Ndarray* s0 = inputs[1];
    Ndarray* s1 = inputs[2];
    Ndarray* weight = inputs[3];
    Ndarray* mask = inputs[4];
    Ndarray* out_W = *outputs[0];

    assert(Ndarray_NDIM(s0) == 2);
    assert(Ndarray_NDIM(s1) == 2);
    assert(Ndarray_NDIM(weight) == 2);
    assert(Ndarray_NDIM(mask) == 2);
    assert(Ndarray_NDIM(out_W) == 3);
    int n_sparse_idx = Ndarray_DIMS(s0)[0];
    assert(n_sparse_idx == Ndarray_DIMS(s1)[0]);
    assert(n_sparse_idx == Ndarray_DIMS(weight)[0]);
    assert(n_sparse_idx == Ndarray_DIMS(mask)[0]);
    int n_batch = Ndarray_DIMS(s0)[1];
    assert(n_batch == Ndarray_DIMS(s1)[1]);
    assert(n_batch == Ndarray_DIMS(weight)[1]);
    assert(n_batch == Ndarray_DIMS(mask)[1]);
    assert(n_batch == Ndarray_DIMS(out_W)[1]);
    int n_time = Ndarray_DIMS(out_W)[0];
    int n_dim = Ndarray_DIMS(out_W)[2];

    start_dev_kernel(assign_kernel, (
      Ndarray_DEV_DATA(out_W),
      Ndarray_DEV_DATA(s0),
      Ndarray_DEV_DATA(s1),
      Ndarray_DEV_DATA(weight),
      Ndarray_DEV_DATA(mask),
      n_sparse_idx, n_time, n_batch, n_dim
    ));
  """


def sparse_to_dense(s0, s1, weight, mask, n_time, n_dim):
  assert s0.ndim == 2
  assert s1.ndim == 2
  assert weight.ndim == 2
  assert mask.ndim == 2
  n_batch = s0.shape[1]
  initial_W = T.zeros((n_time, n_batch, n_dim), dtype="float32")
  op = SparseToDense().make_op()
  W = op(initial_W, s0, s1, weight, mask)
  assert isinstance(W, T.Variable)
  return W


def onehot_to_sparse(y, mask):
  assert y.ndim == 2
  assert mask.ndim == 2
  n_time = y.shape[0]
  n_batch = y.shape[1]
  y_t = T.arange(0, n_time, dtype="float32").dimshuffle(0, 'x') + T.zeros((n_time, n_batch), dtype="float32")
  y_i = y
  y_w = T.ones((n_time, n_batch), dtype="float32")
  return y_t, y_i, y_w, mask


def sparse_slice_offset(s0, idx):
  """
  :param s0: 1D tensor, ordered indices for sparse coo-format matrix (without batch)
  :param idx: scalar, index to find in s0
  :return: s0_idx, such that s0[i] >= idx for all i >= s0_idx, s0[i] < idx for all i < s0_idx.
  This assumes that the indices in s0 are ordered.
  """
  mask = s0 < idx
  return T.sum(mask)


def sparse_splice_offset_numpy(s0, idx):
  """
  Like sparse_slice_offset().
  """
  mask = s0 < idx
  return numpy.sum(mask)


class MaxAndArgmaxSparse(NativeOpGenBase):
  """
  Expects a sparse matrix in COOrdinate format,
  where W[s0[i,b],s1[i],b] = weight[i,b] for all i, and all batches b.
  It will return the max and argmax for all W[:,:,b]
  over the second axis.
  """
  in_info = (
    {"name": "s0", "ndim": 2, "shape": (None, None), "need_contiguous": True, "gradient": "disconnected"},
    {"name": "s1", "ndim": 2, "shape": (None, None), "need_contiguous": True, "gradient": "disconnected"},
    {"name": "weight", "ndim": 2, "shape": (None, None), "need_contiguous": True},
    {"name": "mask", "ndim": 2, "shape": (None, None), "need_contiguous": True, "gradient": "disconnected"},
    {"name": "_out_max", "ndim": 2, "shape": (None, None), "need_contiguous": True, "want_inplace": 0, "gradient": "disconnected"},
    {"name": "_out_arg", "ndim": 2, "shape": (None, None), "need_contiguous": True, "want_inplace": 1, "gradient": "disconnected"},
  )
  out_info = (
    {"name": "out_max", "ndim": 2, "shape": ((4, 0), (4, 1))},
    {"name": "out_arg", "ndim": 2, "shape": ((5, 0), (5, 1))},
  )

  c_extra_support_code = {
    "doit_kernel": """
    DEF_KERNEL
    void doit_kernel(
        long n_batch, long n_in_time, long n_out_time,
        float* s0, float* s1, float* weight, float* mask,
        float* out_max, float* out_arg) {
      long batch_idx = threadIdx.x + blockDim.x * blockIdx.x;
      while(batch_idx < n_batch) {
        for(long i = 0; i < n_in_time; ++i) {
          long idx = i * n_batch + batch_idx;
          if(mask[idx] < 0.1) continue;
          long t = (long) s0[idx];
          long j = (long) s1[idx];
          float w = weight[idx];
          if(t < 0 || t >= n_out_time) continue;  // error somehow?
          long out_idx = t * n_batch + batch_idx;
          if(w > out_max[out_idx]) {
            out_max[out_idx] = w;
            out_arg[out_idx] = (float) j;
          }
        }
        batch_idx += gridDim.x * blockDim.x;
      }
    }
    """
  }

  c_fw_code = """
    assert(n_inputs == 6);
    assert(n_outputs == 2);
    Ndarray* s0 = inputs[0];
    Ndarray* s1 = inputs[1];
    Ndarray* weight = inputs[2];
    Ndarray* mask = inputs[3];
    Ndarray* out_max = *outputs[0];
    Ndarray* out_arg = *outputs[1];

    assert(Ndarray_NDIM(s0) == 2);
    assert(Ndarray_NDIM(s1) == 2);
    assert(Ndarray_NDIM(weight) == 2);
    assert(Ndarray_NDIM(mask) == 2);
    assert(Ndarray_NDIM(out_max) == 2);
    assert(Ndarray_NDIM(out_arg) == 2);
    int n_in_time = Ndarray_DIMS(s0)[0];
    assert(n_in_time == Ndarray_DIMS(s1)[0]);
    assert(n_in_time == Ndarray_DIMS(weight)[0]);
    assert(n_in_time == Ndarray_DIMS(mask)[0]);
    int n_batch = Ndarray_DIMS(s0)[1];
    assert(n_batch == Ndarray_DIMS(s1)[1]);
    assert(n_batch == Ndarray_DIMS(weight)[1]);
    assert(n_batch == Ndarray_DIMS(mask)[1]);
    assert(n_batch == Ndarray_DIMS(out_arg)[1]);
    assert(n_batch == Ndarray_DIMS(out_max)[1]);
    int n_out_time = Ndarray_DIMS(out_arg)[0];
    assert(n_out_time == Ndarray_DIMS(out_max)[0]);
    assert(out_max != out_arg);  // earlier bug in NativeOp

    start_dev_kernel(doit_kernel, (
      n_batch, n_in_time, n_out_time,
      Ndarray_DEV_DATA(s0),
      Ndarray_DEV_DATA(s1),
      Ndarray_DEV_DATA(weight),
      Ndarray_DEV_DATA(mask),
      Ndarray_DEV_DATA(out_max),
      Ndarray_DEV_DATA(out_arg)
    ));
  """

  code_version = ()


def max_and_argmax_sparse(s0, s1, weight, mask, out_max, out_arg):
  op = MaxAndArgmaxSparse().make_op()
  out_max, out_arg = op(s0, s1, weight, mask, out_max, out_arg)
  return out_max, out_arg


class CrossEntropySoftmaxAndGradientZSparse(NativeOpGenBase):
  """
  y_target is given in sparse COOrdinate format.
  We will calculate CE[t,b] = \sum_i y_target[t,b,i] * log(softmax(z[t,b])[i]),
  for any timeframe t and batch b,
  and grad(CE[t,b], z[t,b]) = softmax(z[t,b]) - y_target[t,b].
  We also support an index-mask for z, i.e. for the possible [t,b].
  """
  in_info = (
    {"name": "z", "ndim": 3, "shape": (None, None, None), "need_contiguous": True},
    {"name": "z_mask", "ndim": 2, "shape": (None, None), "need_contiguous": True},
    {"name": "y_target_t", "ndim": 2, "shape": (None, None), "need_contiguous": True},
    {"name": "y_target_i", "ndim": 2, "shape": (None, None), "need_contiguous": True},
    {"name": "y_target_w", "ndim": 2, "shape": (None, None), "need_contiguous": True},
    {"name": "y_target_mask", "ndim": 2, "shape": (None, None), "need_contiguous": True}
  )
  out_info = (
    {"name": "out_ce", "ndim": 2, "shape": ((0, 0), (0, 1))},
    {"name": "out_grad_z", "ndim": 3, "shape": ((0, 0), (0, 1), (0, 2))},
    {"name": "_out_max_z", "ndim": 2, "shape": ((0, 0), (0, 1))}
  )

  c_extra_support_code = {
    "max_kernel": """
    DEF_KERNEL
    void max_kernel(float* out, float* v, float* mask, long stride, long max_idx) {
      for(
        long idx = threadIdx.x + blockDim.x * blockIdx.x;
        idx < max_idx;
        idx += gridDim.x * blockDim.x)
      {
        if(mask[idx] < 0.1)
          continue;
        long start = idx * stride;
        float last_max = v[start];
        out[idx] = last_max;
        for(long i = 1; i < stride; ++i) {
          float cur = v[start + i];
          if(cur > last_max) {
            last_max = cur;
            out[idx] = cur;
          }
        }
      }
    }
    """,
    "softmax_kernel": """
    DEF_KERNEL
    void softmax_kernel(
      float* out_softmax,
      float* z, float* max_z, float* mask,
      long stride, long max_idx)
    {
      for(
        long idx = threadIdx.x + blockDim.x * blockIdx.x;
        idx < max_idx;
        idx += gridDim.x * blockDim.x)
      {
        long start = idx * stride;
        float s = 0;
        for(long i = 0; i < stride; ++i) {
          s += exp(z[start + i] - max_z[idx]);
        }
        if(s < 1e-16) s = 1e-16;
        for(long i = 0; i < stride; ++i) {
          float y = exp(z[start + i] - max_z[idx]) / s;
          out_softmax[start + i] = (mask[idx] > 0.5) ? y : 0;
        }
      }
    }
    """,
    "ce_sm_grad_kernel": """
    DEF_KERNEL
    void ce_sm_grad_kernel(
      float* out_ce, float* out_grad_z,
      float* z, float* max_z, float* z_mask,
      float* s0, float* s1, float* w, float* s_mask,
      long n_time, long n_batch, long n_dim, long n_sparse_index)
    {
      long max_idx = n_batch * n_sparse_index;
      for(
        long idx = threadIdx.x + blockDim.x * blockIdx.x;
        idx < max_idx;
        idx += gridDim.x * blockDim.x)
      {
        if(s_mask[idx] < 0.1) continue;
        long batch = idx % n_batch;
        long t = (long) s0[idx];
        long j = (long) s1[idx];
        float y_target = w[idx];
        if(t < 0 || t >= n_time) continue;  // error somehow?
        if(j < 0 || j >= n_dim) continue;  // error somehow?
        long out_ce_idx = t * n_batch + batch;
        long out_y_idx = t * n_batch * n_dim + batch * n_dim + j;
        // This assumes that out_grad_z is still softmax(z).
        // This also assumes that every [t,j] is only represented once in the sparse data.
        out_ce[out_ce_idx] -= y_target * log(fmax(out_grad_z[out_y_idx], 1e-30f));
        out_grad_z[out_y_idx] -= y_target;
      }
    }
    """
  }

  c_fw_code = """
    assert(n_inputs == 6);
    assert(n_outputs == 3);
    Ndarray* z = inputs[0];
    Ndarray* z_mask = inputs[1];
    Ndarray* s0 = inputs[2];
    Ndarray* s1 = inputs[3];
    Ndarray* w = inputs[4];
    Ndarray* s_mask = inputs[5];
    Ndarray* out_ce = *outputs[0];
    Ndarray* out_grad_z = *outputs[1];
    Ndarray* out_max_z = *outputs[2];

    assert(Ndarray_NDIM(z) == 3);
    assert(Ndarray_NDIM(z_mask) == 2);
    assert(Ndarray_NDIM(out_ce) == 2);
    assert(Ndarray_NDIM(out_grad_z) == 3);
    assert(Ndarray_NDIM(out_max_z) == 2);
    assert(Ndarray_NDIM(s0) == 2);
    assert(Ndarray_NDIM(s1) == 2);
    assert(Ndarray_NDIM(w) == 2);
    assert(Ndarray_NDIM(out_ce) == 2);
    int n_time = Ndarray_DIMS(z)[0];
    int n_batch = Ndarray_DIMS(z)[1];
    int n_dim = Ndarray_DIMS(z)[2];
    assert(n_time == Ndarray_DIMS(z_mask)[0]);
    assert(n_time == Ndarray_DIMS(out_ce)[0]);
    assert(n_time == Ndarray_DIMS(out_grad_z)[0]);
    assert(n_time == Ndarray_DIMS(out_max_z)[0]);
    assert(n_batch == Ndarray_DIMS(z_mask)[1]);
    assert(n_batch == Ndarray_DIMS(out_ce)[1]);
    assert(n_batch == Ndarray_DIMS(out_grad_z)[1]);
    assert(n_batch == Ndarray_DIMS(out_max_z)[1]);
    assert(n_batch == Ndarray_DIMS(s0)[1]);
    assert(n_batch == Ndarray_DIMS(s1)[1]);
    assert(n_batch == Ndarray_DIMS(w)[1]);
    assert(n_batch == Ndarray_DIMS(s_mask)[1]);
    assert(n_dim == Ndarray_DIMS(out_grad_z)[2]);
    int n_sparse_index = Ndarray_DIMS(s0)[0];
    assert(n_sparse_index == Ndarray_DIMS(s1)[0]);
    assert(n_sparse_index == Ndarray_DIMS(w)[0]);
    assert(n_sparse_index == Ndarray_DIMS(s_mask)[0]);

    start_dev_kernel(max_kernel, (
      Ndarray_DEV_DATA(out_max_z), Ndarray_DEV_DATA(z), Ndarray_DEV_DATA(z_mask),
      n_dim, n_time * n_batch
    ));
    Ndarray_set_zero(out_ce);
    start_dev_kernel(softmax_kernel, (
      Ndarray_DEV_DATA(out_grad_z),
      Ndarray_DEV_DATA(z), Ndarray_DEV_DATA(out_max_z), Ndarray_DEV_DATA(z_mask),
      n_dim, n_time * n_batch
    ));
    start_dev_kernel(ce_sm_grad_kernel, (
      Ndarray_DEV_DATA(out_ce), Ndarray_DEV_DATA(out_grad_z),
      Ndarray_DEV_DATA(z), Ndarray_DEV_DATA(out_max_z), Ndarray_DEV_DATA(z_mask),
      Ndarray_DEV_DATA(s0), Ndarray_DEV_DATA(s1), Ndarray_DEV_DATA(w), Ndarray_DEV_DATA(s_mask),
      n_time, n_batch, n_dim, n_sparse_index
    ));
  """


def crossentropy_softmax_and_gradient_z_sparse(z, z_mask, y_target_t, y_target_i, y_target_w, y_target_mask):
  op = CrossEntropySoftmaxAndGradientZSparse().make_op()
  out_ce, out_grad_z, _out_max_z = op(z, z_mask, y_target_t, y_target_i, y_target_w, y_target_mask)
  return out_ce, out_grad_z


def crossentropy_softmax_and_gradient_z_sparse__slow(z, z_mask, y_target_t, y_target_i, y_target_w, y_target_mask):
  assert z.ndim == 3
  n_time = z.shape[0]
  n_batch = z.shape[1]
  n_dim = z.shape[2]
  y_target = sparse_to_dense(y_target_t, y_target_i, y_target_w, y_target_mask, n_time, n_dim)
  y = softmax(z)
  ce = -T.sum(y_target * T.log(y), axis=2)
  grad_z = y - y_target
  return ce, grad_z

common_fast_bw_kernels = {
  "001_set_start_states" : """
    __global__
    void set_start_states(float* states, unsigned* start_states) {
      unsigned state_idx = start_states[blockIdx.x * blockDim.x + threadIdx.x];
      states[state_idx] = 0.0;
    }
  """,
  "010_fill_array" : """
    __global__
    void fill_array(float* array, float value, unsigned size) {
      unsigned idx = blockIdx.x * blockDim.x + threadIdx.x;
      if (idx < size) {
        array[idx] = value;
      }
    }
  """,
  "011_remove_inf": """
  __global__
  void remove_inf(float* array, unsigned size) {
    unsigned idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx < size) {
      array[idx] = fminf(array[idx], 1e32);
    }
  }
  """,
  "012_prob_add": """
    __device__
    float prob_add(float a, float b) {
      float diff = a - b;
      if (isnan(diff)) {
        return CUDART_INF_F;
      }
      else {
        return -log1p(exp(-abs(diff))) + min(a, b);
      }
    }
  """,
  "013_atomic_prob_add": """
    __device__
    void atomic_prob_add(float* a, float b) {
      int* addr = (int*)a;
      int old   = __float_as_int(*a);
      int assumed;
      do {
        assumed = old;
        old     = atomicCAS(addr, assumed, __float_as_int(prob_add(__int_as_float(old), b)));
      } while (old != assumed);
    }
  """,
  "020_dump_to_file": """
    template<typename T>
    void dump_to_file_1d(T* d_mem, unsigned n_d1, std::string const& path) {
      std::vector<T> buffer(n_d1);
      cudaMemcpy(buffer.data(), d_mem, buffer.size() * sizeof(T), cudaMemcpyDeviceToHost);

      std::ofstream output(path.c_str(), std::ios::trunc | std::ios::out);
      for (size_t i1 = 0ul; i1 < n_d1; i1++) {
        T val = buffer[i1];
        if (!std::numeric_limits<T>::has_infinity or !std::isinf(val)) {
          output << i1 << ' ' << val << '\\n';
        }
      }
    }

    template<typename T>
    void dump_to_file_2d(T* d_mem, unsigned n_d1, unsigned n_d2, std::string const& path) {
      std::vector<T> buffer(n_d1 * n_d2);
      cudaMemcpy(buffer.data(), d_mem, buffer.size() * sizeof(T), cudaMemcpyDeviceToHost);

      std::ofstream output(path.c_str(), std::ios::trunc | std::ios::out);
      for (size_t i1 = 0ul; i1 < n_d1; i1++) {
        for (size_t i2 = 0ul; i2 < n_d2; i2++) {
          T val = buffer[i1 * n_d2 + i2];
          if (!std::numeric_limits<T>::has_infinity or !std::isinf(val)) {
            output << i1 << ' ' << i2 << ' ' << val << '\\n';
          }
        }
      }
    }

    template<typename T>
    void dump_to_file_3d(T* d_mem, unsigned n_d1, unsigned n_d2, unsigned n_d3, std::string const& path) {
      std::vector<T> buffer(n_d1 * n_d2 * n_d3);
      cudaMemcpy(buffer.data(), d_mem, buffer.size() * sizeof(T), cudaMemcpyDeviceToHost);

      std::ofstream output(path.c_str(), std::ios::trunc | std::ios::out);
      for (size_t i1 = 0ul; i1 < n_d1; i1++) {
        for (size_t i2 = 0ul; i2 < n_d2; i2++) {
          for (size_t i3 = 0ul; i3 < n_d3; i3++) {
            T val = buffer[i1 * n_d2 * n_d3 + i2 * n_d3 + i3];
            if (!std::numeric_limits<T>::has_infinity or !std::isinf(val)) {
              output << i1 << ' ' << i2 << ' ' << i3 << ' ' << val << '\\n';
            }
          }
        }
      }
    }
  """,
}

class FastBaumWelchOp(NativeOpGenBase):
  """
  inputs:
    :param am_scores: scores in -log space. 3d (time,batch,dim)
    :param edges: edges of the graph (from,to,emission_idx,sequence_idx)
    :param weights: weights of the edges
  outputs:
    :param output: Baum-Welch alignment, scores in -log space. 3d (time,batch,dim), like am_scores
  """
  in_info = (
    {"name": "am_scores",        "ndim": 3, "shape": (None,   None,    None), "need_contiguous": True, "gradient": "disconnected"},
    {"name": "edges",            "ndim": 2, "shape": (None,   None),          "dtype": "int32", "need_contiguous": True, "gradient": "disconnected"},
    {"name": "weights",          "ndim": 1, "shape": (None,),                 "need_contiguous": True, "gradient": "disconnected"},
    {"name": "start_end_states", "ndim": 2, "shape": (2,      None),          "dtype": "int32", "need_contiguous": True, "gradient": "disconnected"},
    {"name": "index",            "ndim": 2, "shape": ((0, 0), (0, 1)),        "need_contiguous": True, "gradient": "disconnected"},
    {"name": "state_buffer",     "ndim": 2, "shape": (2,      None),          "need_contiguous": True, "gradient": "disconnected"}
  )
  out_info = (
    {"name": "output", "ndim": 3, "shape": ((0, 0), (0, 1), (0, 2)), "need_contiguous": True },
    {"name": "sums",   "ndim": 2, "shape": ((0, 0), (0, 1)),         "need_contiguous": True },
  )

  c_extra_support_code = copy.copy(common_fast_bw_kernels)
  c_extra_support_code.update({
    "100_init_bwd_state_buffer": """
      __global__
      void init_bwd_state_buffer(float* states, unsigned* end_states, unsigned t, unsigned max_t, float* index, unsigned index_stride) {
        unsigned idx = blockIdx.x * blockDim.x + threadIdx.x;
        if (index[t * index_stride + idx] == 1.0 && (t == max_t || index[(t + 1) * index_stride + idx] == 0.0)) {
          unsigned state_idx = end_states[idx];
          states[state_idx] = 0.0;
        }
      }
    """,
    "101_next_frame": """
      __global__
      void next_frame(bool fwd, unsigned num_edges, unsigned  num_emissions,
                      unsigned* sequence_idxs, unsigned* from_buffer, unsigned* to_buffer, float* weight_buffer, unsigned* emission_idxs,
                      float* prev_frame, float* next_frame, float* am_scores, float* edge_buffer) {
        unsigned idx = blockIdx.x * blockDim.x + threadIdx.x;
        if (idx >= num_edges) {
          return;
        }

        unsigned from     = from_buffer  [idx];
        float    prev_val = prev_frame[from];
        if (isinf(prev_val)) {
          edge_buffer[idx] = CUDART_INF_F;
          return;
        }

        unsigned to           = to_buffer    [idx];
        unsigned emission_idx = emission_idxs[idx];
        float    edge_weight  = weight_buffer[idx];
        unsigned sequence_idx = sequence_idxs[idx];

        float val = prev_val + edge_weight + am_scores[sequence_idx * num_emissions + emission_idx];

        if (fwd) {
          edge_buffer[idx] += val;
        }
        else {
          edge_buffer[idx] += prev_val;
        }
        atomic_prob_add(next_frame + to, val);
      }
    """,
    "102_normalize": """
      __global__
      void normalize(float* buffer, unsigned* sequence_idxs, unsigned num_edges, unsigned num_seqs, float* sum_output) {
        extern __shared__ float sum[];

        buffer += blockIdx.x * num_edges;

        for (unsigned s = 0u; s < num_seqs; s++) {
          sum[s] = CUDART_INF_F;
        }

        for (unsigned e = 0u; e < num_edges; e++) {
          unsigned s = sequence_idxs[e];
          sum[s] = prob_add(sum[s], buffer[e]);
        }

        for (unsigned s = 0ul; s < num_seqs; s++) {
          if (isinf(sum[s])) {
            // if the frame is empty (happens due to batching of seqs with unequal length), set it to 0
            sum_output[blockIdx.x * num_seqs + s] = 0.0;
          }
          else {
            sum_output[blockIdx.x * num_seqs + s] = sum[s];
          }
        }

        for (unsigned e = 0u; e < num_edges; e++) {
          unsigned s = sequence_idxs[e];
          buffer[e] -= sum[s];
        }
      }
    """,
    "103_compute_result": """
      __global__
      void compute_result(float* edge_buffer, float* out, unsigned* emission_idxs, unsigned* sequence_idxs,
                          unsigned frame_stride, unsigned seq_stride,
                          unsigned num_frames, unsigned num_seqs, unsigned num_edges) {
        unsigned idx = blockIdx.x * blockDim.x + threadIdx.x;
        if (idx >= num_frames * num_edges) {
          return;
        }

        unsigned e_idx        = idx % num_edges;
        unsigned frame        = idx / num_edges;
        unsigned emission_idx = emission_idxs[e_idx];
        unsigned seq_idx      = sequence_idxs[e_idx];
        float    score        = edge_buffer[idx];

        atomic_prob_add(out + frame * frame_stride + seq_idx * seq_stride + emission_idx, score);
      }
    """,
    "110_write_alignment_to_file": """
      void write_alignment_to_file(float* d_state_buffer, float* d_index, unsigned index_stride,
                                   unsigned* d_start_states, unsigned* d_end_states,
                                   float pruning, unsigned n_frames, unsigned n_seqs, unsigned n_states, unsigned batch_idx) {
        std::vector<float>    state_buffer((n_frames + 1u) * n_states);
        std::vector<float>    index       (n_frames * index_stride);
        std::vector<unsigned> start_states(n_seqs);
        std::vector<unsigned> end_states  (n_seqs);

        HANDLE_ERROR(cudaMemcpy(state_buffer.data(), d_state_buffer, state_buffer.size() * sizeof(float), cudaMemcpyDeviceToHost));
        HANDLE_ERROR(cudaMemcpy(index.data(),        d_index,        index.size()        * sizeof(float), cudaMemcpyDeviceToHost));
        HANDLE_ERROR(cudaMemcpy(start_states.data(), d_start_states, start_states.size() * sizeof(float), cudaMemcpyDeviceToHost));
        HANDLE_ERROR(cudaMemcpy(end_states.data(),   d_end_states,   end_states.size()   * sizeof(float), cudaMemcpyDeviceToHost));

        for (unsigned seq = 0u; seq < n_seqs; seq++) {
          std::stringstream filename;
          filename << "alignment.dump." << batch_idx << '.' << seq;
          std::ofstream out(filename.str().c_str(), std::ios::out | std::ios::trunc);
          for (unsigned t = 0u; t <= n_frames; t++) {
            if (t > 0u and index[seq * index_stride + t] <= 0.0) {
              break;
            }
            float sum = std::numeric_limits<float>::infinity();
            for (unsigned s = start_states[seq]; s <= end_states[seq]; s++) {
              const float val = state_buffer[t * n_states + s];
              float diff = val - sum;
              if (!isnan(diff)) {
                sum = -log1p(exp(-abs(diff))) + min(sum, val);
              }
            }
            for (unsigned s = start_states[seq]; s <= end_states[seq]; s++) {
              const float val = state_buffer[t * n_states + s] - sum;
              if (val <= pruning) {
                out << t << ' ' << (s - start_states[seq]) << ' ' << val << '\\n';
              }
            }
          }
        }
      }
    """,
    "111_write_output_to_file": """
      void write_output_to_file(float* d_out, float* d_index, unsigned index_stride,
                                float pruning, unsigned n_frames, unsigned n_seqs, unsigned n_emissions, unsigned batch_idx) {
        std::vector<float> buffer(n_frames * n_seqs * n_emissions);
        std::vector<float> index (n_frames * index_stride);

        HANDLE_ERROR(cudaMemcpy(buffer.data(), d_out,   buffer.size() * sizeof(float), cudaMemcpyDeviceToHost));
        HANDLE_ERROR(cudaMemcpy(index.data(),  d_index, index.size()  * sizeof(float), cudaMemcpyDeviceToHost));

        for (unsigned seq = 0u; seq < n_seqs; seq++) {
          std::stringstream filename;
          filename << "target.dump." << batch_idx << '.' << seq;
          std::ofstream out(filename.str().c_str(), std::ios::out | std::ios::trunc);
          for (unsigned t = 0u; t <= n_frames; t++) {
            if (t > 0u and index[seq * index_stride + t] <= 0.0) {
              break;
            }
            for (unsigned e = 0u; e < n_emissions; e++) {
              const float val = buffer[t * n_seqs * n_emissions + seq * n_emissions + e];
              if (val <= pruning) {
                out << t << ' ' << e << ' ' << val << '\\n';
              }
            }
          }
        }
      }
    """,
  })

  c_fw_code = """
    // am_scores, edges, weights, start_end_states, index, state_buffer* = input_names (*: inplace)
    // output = output_names
    assert(n_inputs  == 6);
    assert(n_outputs == 2);
    Ndarray* am_scores        = inputs[0];
    Ndarray* edges            = inputs[1];
    Ndarray* weights          = inputs[2];
    Ndarray* start_end_states = inputs[3];
    Ndarray* index            = inputs[4];
    Ndarray* state_buffer     = inputs[5];
    Ndarray* out              = *outputs[0];
    Ndarray* sum_output       = *outputs[1];

    /*
    debug_print(context, am_scores, "am_scores");
    debug_print(context, edges, "edges");
    debug_print(context, weights, "weights");
    debug_print(context, start_end_states, "start_end_states");
    debug_print(context, index, "index");
    debug_print(context, state_buffer, "state_buffer");
    */

    assert(Ndarray_DIMS(am_scores)[0] == Ndarray_DIMS(out)[0]);
    assert(Ndarray_DIMS(am_scores)[1] == Ndarray_DIMS(out)[1]);
    assert(Ndarray_DIMS(am_scores)[2] == Ndarray_DIMS(out)[2]);
    assert(Ndarray_DIMS(am_scores)[1] == Ndarray_DIMS(start_end_states)[1]);

    assert(Ndarray_DIMS(sum_output)[0] == Ndarray_DIMS(am_scores)[0]);
    assert(Ndarray_DIMS(sum_output)[1] == Ndarray_DIMS(am_scores)[1]);

    bool            dump_alignment = false;
    bool            dump_output    = false;
    unsigned        dump_every = 40u;
    static unsigned batch_idx  = 0u;
    float           pruning    = 10.f;

    unsigned* d_from              = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA_int32(edges) + 0 * Ndarray_STRIDE(edges, 0));
    unsigned* d_to                = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA_int32(edges) + 1 * Ndarray_STRIDE(edges, 0));
    unsigned* d_emission_idxs     = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA_int32(edges) + 2 * Ndarray_STRIDE(edges, 0));
    unsigned* d_sequence_idxs     = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA_int32(edges) + 3 * Ndarray_STRIDE(edges, 0));
    float*    d_weights           = Ndarray_DEV_DATA(weights);
    float*    d_am_scores         = Ndarray_DEV_DATA(am_scores);
    unsigned* d_start_states      = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA_int32(start_end_states) + 0 * Ndarray_STRIDE(start_end_states, 0));
    unsigned* d_end_states        = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA_int32(start_end_states) + 1 * Ndarray_STRIDE(start_end_states, 0));
    float*    d_index             = Ndarray_DEV_DATA(index);
    float*    d_state_buffer_prev = Ndarray_DEV_DATA(state_buffer) + 0 * Ndarray_STRIDE(state_buffer, 0);
    float*    d_state_buffer_next = Ndarray_DEV_DATA(state_buffer) + 1 * Ndarray_STRIDE(state_buffer, 0);
    float*    d_out               = Ndarray_DEV_DATA(out);
    float*    d_sum_output        = Ndarray_DEV_DATA(sum_output);

    unsigned n_frames    = Ndarray_DIMS(am_scores)[0];
    unsigned n_seqs      = Ndarray_DIMS(am_scores)[1];
    unsigned n_emissions = Ndarray_DIMS(am_scores)[2];
    unsigned n_states    = Ndarray_DIMS(state_buffer)[1];
    unsigned n_edges     = Ndarray_DIMS(edges)[1];
    unsigned n_threads   = 1024u;
    unsigned n_blocks    = (n_edges + n_threads - 1) / n_threads;

    unsigned frame_stride    = Ndarray_STRIDE(am_scores, 0);
    unsigned sequence_stride = Ndarray_STRIDE(am_scores, 1);
    unsigned index_stride    = Ndarray_STRIDE(index, 0);

    assert(n_frames > 0);

    //std::cerr << "n_frames: "    << n_frames    << std::endl;
    //std::cerr << "n_seqs: "      << n_seqs      << std::endl;
    //std::cerr << "n_emissions: " << n_emissions << std::endl;
    //std::cerr << "n_states: "    << n_states    << std::endl;
    //std::cerr << "n_edges: "     << n_edges     << std::endl;
    //std::cerr << "n_threads: "   << n_threads   << std::endl;
    //std::cerr << "n_blocks: "    << n_blocks    << std::endl;

    //std::cerr << "frame_stride: "     << frame_stride    << std::endl;
    //std::cerr << "sequnence_stride: " << sequence_stride << std::endl;
    //std::cerr << "index_stride: "     << index_stride    << std::endl;

    // initialize edge buffer
    float* d_edge_buffer = reinterpret_cast<float*>(device_malloc(n_edges * n_frames * sizeof(float)));
    unsigned n_fill_blocks = (n_edges * n_frames + n_threads - 1u) / n_threads;
    fill_array<<<n_fill_blocks, n_threads>>>(d_edge_buffer, 0.0, n_edges * n_frames);
    HANDLE_LAST_ERROR();

    // initialize the state buffer
    n_fill_blocks = (n_states + n_threads - 1u) / n_threads;
    fill_array<<<n_fill_blocks, n_threads>>>(d_state_buffer_prev, std::numeric_limits<float>::infinity(), n_states);
    HANDLE_LAST_ERROR();
    set_start_states<<<1, n_seqs>>>(d_state_buffer_prev, d_start_states);

    // initialize full state buffer (only used to dump the alignment)
    float* d_state_buffer_all = NULL;
    if (dump_alignment and batch_idx %% dump_every == 0) {
      d_state_buffer_all = reinterpret_cast<float*>(device_malloc(n_states * (n_frames + 1u) * sizeof(float)));
      cudaMemcpy(d_state_buffer_all, d_state_buffer_prev, n_states * sizeof(float), cudaMemcpyDeviceToDevice);
    }

    // fwd pass
    for (unsigned t = 0u; t < n_frames; t++) {
      fill_array<<<n_fill_blocks, n_threads>>>(d_state_buffer_next, std::numeric_limits<float>::infinity(), n_states);
      HANDLE_LAST_ERROR();
      next_frame<<<n_blocks, n_threads>>>(true, n_edges, sequence_stride,
                                          d_sequence_idxs, d_from, d_to, d_weights, d_emission_idxs,
                                          d_state_buffer_prev, d_state_buffer_next, d_am_scores + t * frame_stride, d_edge_buffer + t * n_edges);
      HANDLE_LAST_ERROR();
      if (dump_alignment and batch_idx %% dump_every == 0) {
        cudaMemcpy(d_state_buffer_all + (t + 1u) * n_states, d_state_buffer_next, n_states * sizeof(float), cudaMemcpyDeviceToDevice);
      }
      std::swap(d_state_buffer_prev, d_state_buffer_next);
    }

    // bwd pass
    fill_array<<<n_fill_blocks, n_threads>>>(d_state_buffer_prev, std::numeric_limits<float>::infinity(), n_states);
    HANDLE_LAST_ERROR();
    for (unsigned t = n_frames; t > 0; t--) {
      init_bwd_state_buffer<<<1, n_seqs>>>(d_state_buffer_prev, d_end_states, t - 1, n_frames - 1, d_index, index_stride);
      HANDLE_LAST_ERROR();
      if (dump_alignment and batch_idx %% dump_every == 0) {
        float alpha = 1.0f;
        HANDLE_ERROR(cublasSaxpy(handle, n_states, &alpha, d_state_buffer_prev, 1, d_state_buffer_all + t * n_states, 1));
      }
      fill_array<<<n_fill_blocks, n_threads>>>(d_state_buffer_next, std::numeric_limits<float>::infinity(), n_states);
      HANDLE_LAST_ERROR();
      next_frame<<<n_blocks, n_threads>>>(false, n_edges, sequence_stride,
                                          d_sequence_idxs, d_to, d_from, d_weights, d_emission_idxs,
                                          d_state_buffer_prev, d_state_buffer_next, d_am_scores + (t - 1) * frame_stride, d_edge_buffer + (t - 1) * n_edges);
      HANDLE_LAST_ERROR();
      std::swap(d_state_buffer_prev, d_state_buffer_next);
    }
    if (dump_alignment and batch_idx %% dump_every == 0) {
      float alpha = 1.0f;
      HANDLE_ERROR(cublasSaxpy(handle, n_states, &alpha, d_state_buffer_prev, 1, d_state_buffer_all, 1));
    }

    // normalize at each time frame
    normalize<<<n_frames, 1, n_seqs * sizeof(float)>>>(d_edge_buffer, d_sequence_idxs, n_edges, n_seqs, d_sum_output);
    HANDLE_LAST_ERROR();

    // dump alignment
    if (dump_alignment and batch_idx %% dump_every == 0) {
      write_alignment_to_file(d_state_buffer_all, d_index, index_stride, d_start_states, d_end_states,
                              pruning, n_frames, n_seqs, n_states, batch_idx);
    }

    n_fill_blocks = (n_frames * n_seqs * n_emissions + n_threads - 1u) / n_threads;
    fill_array<<<n_fill_blocks, n_threads>>>(d_out, std::numeric_limits<float>::infinity(), n_frames * n_seqs * n_emissions);
    HANDLE_LAST_ERROR();

    frame_stride    = Ndarray_STRIDE(out, 0);
    sequence_stride = Ndarray_STRIDE(out, 1);
    n_blocks        = (n_frames * n_edges + n_threads - 1u) / n_threads;
    compute_result<<<n_blocks, n_threads>>>(d_edge_buffer, d_out, d_emission_idxs, d_sequence_idxs,
                                            frame_stride, sequence_stride, n_frames, n_seqs, n_edges);
    HANDLE_LAST_ERROR();

    #if TENSORFLOW
    // Certain TensorFlow code doesn't like inf, even if it is just the CheckNumerics,
    // which is helpful for debugging.
    // We replace it by a very high number, so that tf.exp(-out) will still result in 0.0.
    n_blocks = (n_frames * n_seqs * n_emissions + n_threads - 1u) / n_threads;
    remove_inf<<<n_blocks, n_threads>>>(d_out, n_frames * n_seqs * n_emissions);
    //debug_print(context, out, "out");
    #endif
    if (dump_output and batch_idx %% dump_every == 0) {
      write_output_to_file(d_out, d_index, index_stride, pruning, n_frames, n_seqs, n_emissions, batch_idx);
    }

    device_free(d_edge_buffer);
    if (d_state_buffer_all != NULL) {
      device_free(d_state_buffer_all);
    }
    batch_idx++;
  """

  c_bw_code = None

  cpu_support = False  # TODO: fix CPU support...

class MultiEndFastBaumWelchOp(NativeOpGenBase):
  """
  inputs:
    :param am_scores: scores in -log space. 3d (time,batch,dim)
    :param edges: edges of the graph (from,to,emission_idx,sequence_idx)
    :param weights: weights of the edges
  outputs:
    :param output: Baum-Welch alignment, scores in -log space. 3d (time,batch,dim), like am_scores
  """
  in_info = (
    {"name": "am_scores",         "ndim": 3, "shape": (None,   None,    None), "need_contiguous": True, "gradient": "disconnected"},
    {"name": "edges",             "ndim": 2, "shape": (None,   None),          "need_contiguous": True, "gradient": "disconnected", "dtype": "int32"},
    {"name": "weights",           "ndim": 1, "shape": (None,),                 "need_contiguous": True, "gradient": "disconnected"},
    {"name": "start_states",      "ndim": 1, "shape": (None),                  "need_contiguous": True, "gradient": "disconnected", "dtype": "int32"},
    {"name": "end_states",        "ndim": 2, "shape": (None, 2),               "need_contiguous": True, "gradient": "disconnected", "dtype": "int32"},
    {"name": "end_state_weights", "ndim": 1, "shape": ((4, 0)),                "need_contiguous": True, "gradient": "disconnected"},
    {"name": "index",             "ndim": 2, "shape": ((0, 0), (0, 1)),        "need_contiguous": True, "gradient": "disconnected"},
    {"name": "state_buffer",      "ndim": 2, "shape": (2,      None),          "need_contiguous": True, "gradient": "disconnected"}
  )
  out_info = (
    {"name": "output", "ndim": 3, "shape": ((0, 0), (0, 1), (0, 2)), "need_contiguous": True },
    {"name": "sums",   "ndim": 2, "shape": ((0, 0), (0, 1)),         "need_contiguous": True },
  )

  c_extra_support_code = copy.copy(FastBaumWelchOp.c_extra_support_code)
  c_extra_support_code.update({
    "100_init_bwd_state_buffer": """
      __global__
      void init_bwd_state_buffer(float* states, unsigned* end_states, float* end_state_weigths, unsigned t, unsigned max_t, float* index, unsigned index_stride) {
        unsigned idx = blockIdx.x * blockDim.x + threadIdx.x;
        unsigned seq_idx = end_states[idx * 2u + 0u];
        if (index[t * index_stride + seq_idx] == 1.0 && (t == max_t || index[(t + 1) * index_stride + seq_idx] == 0.0)) {
          unsigned state_idx = end_states[idx * 2u + 1u];
          float    weight    = end_state_weights[idx];
          states[state_idx] = weight;
        }
      }
    """})

  c_fw_code = """
    // am_scores, edges, weights, start_states, end_states, end_state_weigths index, state_buffer* = input_names (*: inplace)
    // output = output_names
    assert(n_inputs  == 8);
    assert(n_outputs == 2);
    Ndarray* am_scores         = inputs[0];
    Ndarray* edges             = inputs[1];
    Ndarray* weights           = inputs[2];
    Ndarray* start_states      = inputs[3];
    Ndarray* end_states        = inputs[4];
    Ndarray* end_state_weights = inputs[5];
    Ndarray* index             = inputs[6];
    Ndarray* state_buffer      = inputs[7];
    Ndarray* out               = *outputs[0];
    Ndarray* sum_output        = *outputs[1];

    assert(Ndarray_DIMS(am_scores)[0] == Ndarray_DIMS(out)[0]);
    assert(Ndarray_DIMS(am_scores)[1] == Ndarray_DIMS(out)[1]);
    assert(Ndarray_DIMS(am_scores)[2] == Ndarray_DIMS(out)[2]);
    assert(Ndarray_DIMS(am_scores)[1] == Ndarray_DIMS(start_end_states)[1]);

    assert(Ndarray_DIMS(sum_output)[0] == Ndarray_DIMS(am_scores)[0]);
    assert(Ndarray_DIMS(sum_output)[1] == Ndarray_DIMS(am_scores)[1]);

    bool            dump_alignment = false;
    bool            dump_output    = false;
    unsigned        dump_every = 40u;
    static unsigned batch_idx  = 0u;
    float           pruning    = 10.f;

    unsigned* d_from              = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA_int32(edges) + 0 * Ndarray_STRIDE(edges, 0));
    unsigned* d_to                = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA_int32(edges) + 1 * Ndarray_STRIDE(edges, 0));
    unsigned* d_emission_idxs     = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA_int32(edges) + 2 * Ndarray_STRIDE(edges, 0));
    unsigned* d_sequence_idxs     = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA_int32(edges) + 3 * Ndarray_STRIDE(edges, 0));
    float*    d_weights           = Ndarray_DEV_DATA(weights);
    float*    d_am_scores         = Ndarray_DEV_DATA(am_scores);
    unsigned* d_start_states      = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA_int32(start_states));
    unsigned* d_end_states        = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA_int32(end_states));
    float*    d_end_state_weigths = Ndarray_DEV_DATA(end_state_weights);
    float*    d_index             = Ndarray_DEV_DATA(index);
    float*    d_state_buffer_prev = Ndarray_DEV_DATA(state_buffer) + 0 * Ndarray_STRIDE(state_buffer, 0);
    float*    d_state_buffer_next = Ndarray_DEV_DATA(state_buffer) + 1 * Ndarray_STRIDE(state_buffer, 0);
    float*    d_out               = Ndarray_DEV_DATA(out);
    float*    d_sum_output        = Ndarray_DEV_DATA(sum_output);

    unsigned n_frames     = Ndarray_DIMS(am_scores)[0];
    unsigned n_seqs       = Ndarray_DIMS(am_scores)[1];
    unsigned n_emissions  = Ndarray_DIMS(am_scores)[2];
    unsigned n_states     = Ndarray_DIMS(state_buffer)[1];
    unsigned n_edges      = Ndarray_DIMS(edges)[1];
    unsigned n_end_states = Ndarray_DIMS(end_states)[0];
    unsigned n_threads    = 1024u;
    unsigned n_blocks     = (n_edges + n_threads - 1) / n_threads;

    unsigned frame_stride    = Ndarray_STRIDE(am_scores, 0);
    unsigned sequence_stride = Ndarray_STRIDE(am_scores, 1);
    unsigned index_stride    = Ndarray_STRIDE(index, 0);

    assert(n_frames > 0);

    //std::cerr << "n_frames: "     << n_frames     << std::endl;
    //std::cerr << "n_seqs: "       << n_seqs       << std::endl;
    //std::cerr << "n_emissions: "  << n_emissions  << std::endl;
    //std::cerr << "n_states: "     << n_states     << std::endl;
    //std::cerr << "n_edges: "      << n_edges      << std::endl;
    //std::cerr << "n_end_states: " << n_end_states << std::endl;
    //std::cerr << "n_threads: "    << n_threads    << std::endl;
    //std::cerr << "n_blocks: "     << n_blocks     << std::endl;

    //std::cerr << "frame_stride: "     << frame_stride    << std::endl;
    //std::cerr << "sequnence_stride: " << sequence_stride << std::endl;
    //std::cerr << "index_stride: "     << index_stride    << std::endl;

    // initialize edge buffer
    float* d_edge_buffer = reinterpret_cast<float*>(device_malloc(n_edges * n_frames * sizeof(float)));
    unsigned n_fill_blocks = (n_edges * n_frames + n_threads - 1u) / n_threads;
    fill_array<<<n_fill_blocks, n_threads>>>(d_edge_buffer, 0.0, n_edges * n_frames);
    HANDLE_LAST_ERROR();

    // initialize the state buffer
    n_fill_blocks = (n_states + n_threads - 1u) / n_threads;
    fill_array<<<n_fill_blocks, n_threads>>>(d_state_buffer_prev, std::numeric_limits<float>::infinity(), n_states);
    HANDLE_LAST_ERROR();
    set_start_states<<<1, n_seqs>>>(d_state_buffer_prev, d_start_states);

    // initialize full state buffer (only used to dump the alignment)
    float* d_state_buffer_all = NULL;
    if (dump_alignment and batch_idx %% dump_every == 0) {
      d_state_buffer_all = reinterpret_cast<float*>(device_malloc(n_states * (n_frames + 1u) * sizeof(float)));
      cudaMemcpy(d_state_buffer_all, d_state_buffer_prev, n_states * sizeof(float), cudaMemcpyDeviceToDevice);
    }

    // fwd pass
    for (unsigned t = 0u; t < n_frames; t++) {
      fill_array<<<n_fill_blocks, n_threads>>>(d_state_buffer_next, std::numeric_limits<float>::infinity(), n_states);
      HANDLE_LAST_ERROR();
      next_frame<<<n_blocks, n_threads>>>(true, n_edges, sequence_stride,
                                          d_sequence_idxs, d_from, d_to, d_weights, d_emission_idxs,
                                          d_state_buffer_prev, d_state_buffer_next, d_am_scores + t * frame_stride, d_edge_buffer + t * n_edges);
      HANDLE_LAST_ERROR();
      if (dump_alignment and batch_idx %% dump_every == 0) {
        cudaMemcpy(d_state_buffer_all + (t + 1u) * n_states, d_state_buffer_next, n_states * sizeof(float), cudaMemcpyDeviceToDevice);
      }
      std::swap(d_state_buffer_prev, d_state_buffer_next);
    }

    // bwd pass
    fill_array<<<n_fill_blocks, n_threads>>>(d_state_buffer_prev, std::numeric_limits<float>::infinity(), n_states);
    HANDLE_LAST_ERROR();
    for (unsigned t = n_frames; t > 0; t--) {
      init_bwd_state_buffer<<<1, n_end_states>>>(d_state_buffer_prev, d_end_states, d_end_state_weigths, t - 1, n_frames - 1, d_index, index_stride);
      HANDLE_LAST_ERROR();
      if (dump_alignment and batch_idx %% dump_every == 0) {
        float alpha = 1.0f;
        HANDLE_ERROR(cublasSaxpy(handle, n_states, &alpha, d_state_buffer_prev, 1, d_state_buffer_all + t * n_states, 1));
      }
      fill_array<<<n_fill_blocks, n_threads>>>(d_state_buffer_next, std::numeric_limits<float>::infinity(), n_states);
      HANDLE_LAST_ERROR();
      next_frame<<<n_blocks, n_threads>>>(false, n_edges, sequence_stride,
                                          d_sequence_idxs, d_to, d_from, d_weights, d_emission_idxs,
                                          d_state_buffer_prev, d_state_buffer_next, d_am_scores + (t - 1) * frame_stride, d_edge_buffer + (t - 1) * n_edges);
      HANDLE_LAST_ERROR();
      std::swap(d_state_buffer_prev, d_state_buffer_next);
    }
    if (dump_alignment and batch_idx %% dump_every == 0) {
      float alpha = 1.0f;
      HANDLE_ERROR(cublasSaxpy(handle, n_states, &alpha, d_state_buffer_prev, 1, d_state_buffer_all, 1));
    }

    // normalize at each time frame
    normalize<<<n_frames, 1, n_seqs * sizeof(float)>>>(d_edge_buffer, d_sequence_idxs, n_edges, n_seqs, d_sum_output);
    HANDLE_LAST_ERROR();

    // dump alignment
    if (dump_alignment and batch_idx %% dump_every == 0) {
      write_alignment_to_file(d_state_buffer_all, d_index, index_stride, d_start_states, d_end_states,
                              pruning, n_frames, n_seqs, n_states, batch_idx);
    }

    n_fill_blocks = (n_frames * n_seqs * n_emissions + n_threads - 1u) / n_threads;
    fill_array<<<n_fill_blocks, n_threads>>>(d_out, std::numeric_limits<float>::infinity(), n_frames * n_seqs * n_emissions);
    HANDLE_LAST_ERROR();

    frame_stride    = Ndarray_STRIDE(out, 0);
    sequence_stride = Ndarray_STRIDE(out, 1);
    n_blocks        = (n_frames * n_edges + n_threads - 1u) / n_threads;
    compute_result<<<n_blocks, n_threads>>>(d_edge_buffer, d_out, d_emission_idxs, d_sequence_idxs,
                                            frame_stride, sequence_stride, n_frames, n_seqs, n_edges);
    HANDLE_LAST_ERROR();

    #if TENSORFLOW
    // Certain TensorFlow code doesn't like inf, even if it is just the CheckNumerics,
    // which is helpful for debugging.
    // We replace it by a very high number, so that tf.exp(-out) will still result in 0.0.
    n_blocks = (n_frames * n_seqs * n_emissions + n_threads - 1u) / n_threads;
    remove_inf<<<n_blocks, n_threads>>>(d_out, n_frames * n_seqs * n_emissions);
    //debug_print(context, out, "out");
    #endif
    if (dump_output and batch_idx %% dump_every == 0) {
      write_output_to_file(d_out, d_index, index_stride, pruning, n_frames, n_seqs, n_emissions, batch_idx);
    }

    device_free(d_edge_buffer);
    if (d_state_buffer_all != NULL) {
      device_free(d_state_buffer_all);
    }
    batch_idx++;
  """

  c_bw_code = None

  cpu_support = False  # TODO: fix CPU support...

class SegmentFastBaumWelchOp(NativeOpGenBase):
  in_info = (
    {"name": "am_scores",        "ndim": 3, "shape": (None,   None,    None), "need_contiguous": True, "gradient": "disconnected"},
    {"name": "batch_idxs",       "ndim": 2, "shape": (None,   None),          "need_contiguous": True, "gradient": "disconnected"},
    {"name": "edges",            "ndim": 2, "shape": (None,   None),          "need_contiguous": True, "gradient": "disconnected"},
    {"name": "weights",          "ndim": 1, "shape": ((2, 1),),               "need_contiguous": True, "gradient": "disconnected"},
    {"name": "length_models",    "ndim": 2, "shape": (None,   (0, 0)),        "need_contiguous": True, "gradient": "disconnected"},
    {"name": "start_end_states", "ndim": 2, "shape": (2,      None),          "need_contiguous": True, "gradient": "disconnected"},
    {"name": "index",            "ndim": 2, "shape": ((0, 0), (0, 1)),        "need_contiguous": True, "gradient": "disconnected"},
    {"name": "am_score_scales",  "ndim": 1, "shape": (None,),                 "need_contiguous": True, "gradient": "disconnected"},
    {"name": "epoch",            "ndim": 0, "shape": tuple(),                 "need_contiguous": True, "gradient": "disconnected"},
  )
  out_info = (
    {"name": "output",                "ndim": 3, "shape": ((0, 0), (0, 1), (0, 2)), "need_contiguous": True },
    {"name": "normalization_factors", "ndim": 2, "shape": ((0, 0), (0, 1)),         "need_contiguous": True },
    {"name": "posterior_weigths",     "ndim": 2, "shape": ((0, 0), (0, 1)),         "need_contiguous": True },
  )

  c_extra_support_code = copy.copy(common_fast_bw_kernels)
  c_extra_support_code.update({
    "100_init_bwd_state_buffer": """
      __global__
      void init_bwd_state_buffer(unsigned t, unsigned num_batches, unsigned num_seqs,
                                 int* batch_idxs, float* index, float* states, unsigned* end_states) {
        unsigned idx = blockIdx.x * blockDim.x + threadIdx.x;
        int batch_idx = batch_idxs[t * num_seqs + idx];
        if (batch_idx < 0) {
          return;
        }
        float* batch_first_frame = index + batch_idx;
        //if (*batch_first_frame != 0.0 && (t == max_t || *(batch_first_frame + 1) == 0.0)) {
        if (batch_first_frame[0] != 0.0 && batch_first_frame[num_batches] == 0.0) {
          unsigned state_idx = end_states[idx];
          states[state_idx] = 0.0;
        }
      }
    """,
    "101_next_frame_fwd": """
      __global__
      void next_frame_fwd(unsigned time, unsigned num_states, unsigned num_edges, unsigned num_emissions, unsigned num_seg_frames,
                          unsigned num_tot_frames, unsigned num_seqs, unsigned num_am_score_scales,
                          unsigned const* sequence_idxs, unsigned const* from_buffer, unsigned const* to_buffer, float const* weight_buffer,
                          unsigned const* emission_idxs, unsigned const* lenmod_idxs, int const* batch_idxs,
                          float const* am_scores, float const* length_models, float const* am_score_scales, float const* epoch,
                          float* state_buffer, float* edge_buffer) {
        const unsigned idx = blockIdx.x * blockDim.x + threadIdx.x;
        if (idx >= num_edges) {
          return;
        }

        const unsigned num_ringbuffer_frames = num_seg_frames + 1;
        const unsigned max_seg_frames        = min(num_seg_frames, num_tot_frames - time);

        const unsigned prev_frame_idx   = time % num_ringbuffer_frames;
        const unsigned prev_frame_start = prev_frame_idx * num_states;

        const unsigned from     = from_buffer [idx];
        const float    prev_val = state_buffer[prev_frame_start + from];
        if (isinf(prev_val)) {
          return;
        }

        const unsigned sequence_idx = sequence_idxs[idx];
        const int      batch_idx    = batch_idxs[time * num_seqs + sequence_idx];
        if (batch_idx == -1) {
          return;
        }

        const unsigned amss_idx       = min(static_cast<unsigned>(*epoch), num_am_score_scales - 1);
        const float    am_score_scale = am_score_scales[amss_idx];

        const unsigned to             = to_buffer    [idx];
        const unsigned emission_idx   = emission_idxs[idx];
        const unsigned lenmod_idx     = lenmod_idxs  [idx];
        const float    edge_weight    = weight_buffer[idx];
        const float    prev_plus_edge = prev_val + edge_weight;

        float const* am_buffer_in    = am_scores     + batch_idx  * num_seg_frames * num_emissions + emission_idx;
        float const* length_scores   = length_models + lenmod_idx * num_seg_frames;
        float*       edge_buffer_out = edge_buffer   + idx;

        for (unsigned i = 0u; i < max_seg_frames; i++) {
          const float val = prev_plus_edge + am_score_scale * am_buffer_in[i * num_emissions] + length_scores[i];
          edge_buffer_out[i * num_edges] = val;
          const unsigned next_frame = (prev_frame_idx + 1 + i) % num_ringbuffer_frames;
          atomic_prob_add(state_buffer + (next_frame * num_states + to), val);
        }
      }
    """,
    "102_next_frame_bwd": """
      __global__
      void next_frame_bwd(unsigned time, unsigned num_states, unsigned num_edges, unsigned num_emissions, unsigned num_seg_frames,
                          unsigned num_tot_frames, unsigned num_seqs, unsigned num_am_score_scales,
                          unsigned const* sequence_idxs, unsigned const* from_buffer, unsigned const* to_buffer, float const* weight_buffer,
                          unsigned const* emission_idxs, unsigned const* lenmod_idxs, int const* batch_idxs,
                          float const* am_scores, float const* length_models, float const* am_score_scales, float const* epoch,
                          float* state_buffer, float* edge_buffer) {
        const unsigned idx = blockIdx.x * blockDim.x + threadIdx.x;
        if (idx >= num_edges) {
          return;
        }

        const unsigned num_ringbuffer_frames = num_seg_frames + 1;
        const unsigned max_seg_frames        = min(num_seg_frames, num_tot_frames - time);

        const unsigned sequence_idx = sequence_idxs[idx];
        const int      batch_idx    = batch_idxs[time * num_seqs + sequence_idx];
        if (batch_idx == -1) {
          return;
        }

        const unsigned amss_idx       = min(static_cast<unsigned>(*epoch), num_am_score_scales - 1);
        const float    am_score_scale = am_score_scales[amss_idx];

        const unsigned from           = from_buffer  [idx];
        const unsigned to             = to_buffer    [idx];
        const unsigned emission_idx   = emission_idxs[idx];
        const unsigned lenmod_idx     = lenmod_idxs  [idx];
        const float    edge_weight    = weight_buffer[idx];
        const unsigned next_frame_idx = time % num_ringbuffer_frames;

        float const*   am_buffer_in    = am_scores     + batch_idx  * num_seg_frames * num_emissions + emission_idx;
        float const*   length_scores   = length_models + lenmod_idx * num_seg_frames;
        float*         edge_buffer_out = edge_buffer   + idx;

        float acc_val = CUDART_INF_F;

        for (unsigned i = 0u; i < max_seg_frames; i++) {
          const unsigned prev_frame_idx = (next_frame_idx + i + 1) % num_ringbuffer_frames;
          const float    prev_val       = state_buffer[prev_frame_idx * num_states + from];
          if (isinf(prev_val)) {
            edge_buffer_out[i * num_edges] = CUDART_INF_F;
          }
          else {
            const float val = prev_val + edge_weight + am_score_scale * am_buffer_in[i * num_emissions] + length_scores[i];
            edge_buffer_out[i * num_edges] += prev_val;
            acc_val = prob_add(acc_val, val);
          }
        }

        atomic_prob_add(state_buffer + next_frame_idx * num_states + to, acc_val);
      }
    """,
    "103_compute_framewise_sum": """
      __global__
      void compute_framewise_sum(unsigned num_tot_frames, unsigned num_seqs, unsigned num_seg_frames, unsigned num_batches, unsigned num_edges,
                                 unsigned const* sequence_idxs, int const* batch_idxs, float const* index, float const* edge_buffer,
                                 float* output_buffer) {
        extern __shared__ float sum[];

        const unsigned idx = blockIdx.x * blockDim.x + threadIdx.x;
        if (idx >= num_tot_frames * num_seg_frames) {
          return;
        }

        float* sum_buffer = sum + threadIdx.x * num_seqs;
        edge_buffer += idx * num_edges;

        for (unsigned s = 0u; s < num_seqs; s++) {
          sum_buffer[s] = CUDART_INF_F;
        }

        for (unsigned i = 0; i < num_edges; i++) {
          const unsigned seq_idx = sequence_idxs[i];
          sum_buffer[seq_idx] = prob_add(sum_buffer[seq_idx], edge_buffer[i]);
        }

        const unsigned time     = idx / num_seg_frames;
        const unsigned seg_size = idx % num_seg_frames;
        for (unsigned s = 0u; s < num_seqs; s++) {
          const int batch_idx = batch_idxs[time * num_seqs + s];
          if (batch_idx >= 0) {
            const unsigned output_idx = seg_size * num_batches + batch_idx;
            if (isinf(sum_buffer[s]) or index[output_idx] == 0.0) {
              output_buffer[output_idx] = 0.0;
            }
            else {
              output_buffer[output_idx] = sum_buffer[s];
            }
          }
        }
      }
    """,
    "104_merge_framewise_sums": """
      __global__
      void merge_framewise_sum(unsigned num_seg_frames, unsigned num_batches, float const* index, float* sum_buffer) {
        const unsigned idx = blockIdx.x * blockDim.x + threadIdx.x;
        if (idx >= num_batches) {
          return;
        }

        sum_buffer += idx;
        index += idx;

        float sum = sum_buffer[0];
        for (unsigned s = 1; s < num_seg_frames; s++) {
          if (index[s * num_batches] != 0.0f) {
            sum = prob_add(sum, sum_buffer[s * num_batches]);
          }
        }

        for (unsigned s = 0; s < num_seg_frames; s++) {
          if (index[s * num_batches] != 0.0f) {
            sum_buffer[s * num_batches] = sum;
          }
        }
      }
    """,
    "105_compute_targets": """
      __global__
      void compute_targets(unsigned num_tot_frames, unsigned num_seg_frames, unsigned num_edges, unsigned num_batches, unsigned num_seqs, unsigned num_emissions,
                           unsigned const* sequence_idxs, unsigned const* emission_idxs, int const* batch_idxs, float const* index,
                           float const* edge_buffer, float const* normalization_buffer, float* output_buffer) {
        const unsigned idx = blockIdx.x * blockDim.x + threadIdx.x;
        if (idx >= num_tot_frames * num_seg_frames * num_edges) {
          return;
        }

        const unsigned edge_idx  = idx % num_edges;
        const unsigned time      = idx / (num_edges * num_seg_frames);
        const unsigned seq_idx   = sequence_idxs[edge_idx];
        const int      batch_idx = batch_idxs[time * num_seqs + seq_idx];

        if (batch_idx < 0) {
          return;
        }

        const unsigned seg_length = (idx / num_edges) % num_seg_frames;

        if (index[seg_length * num_batches + batch_idx] == 0.0) {
          return;
        }

        const unsigned emission_idx  = emission_idxs[edge_idx];
        const float    normalization = normalization_buffer[seg_length * num_batches + batch_idx];

        atomic_prob_add(output_buffer + seg_length * num_batches * num_emissions + batch_idx * num_emissions + emission_idx, edge_buffer[idx] - normalization);
      }
    """,
    "106_compute_posterior_weights": """
    __global__
    void compute_posterior_weights(unsigned num_tot_frames, unsigned num_seg_frames, unsigned num_seqs, unsigned num_batches,
                                   float const* state_buffer, unsigned const* start_states, int const* batch_idxs,
                                   float const* index, float const* normalization_factors, float* posterior_weigths) {
        const unsigned idx = blockIdx.x * blockDim.x + threadIdx.x;
        if (idx >= num_tot_frames * num_seqs) {
          return;
        }

        const unsigned time    = idx / num_seqs;
        const unsigned seq_idx = idx % num_seqs;

        const int batch_idx = batch_idxs[time * num_seqs + seq_idx];
        if (batch_idx < 0) {
          return;
        }

        const float seq_sum = state_buffer[start_states[seq_idx]];
        for (unsigned s = 0u; s < num_seg_frames; s++) {
          const unsigned i = s * num_batches + batch_idx;
          if (index[i] == 0.0) {
            return;
          }
          posterior_weigths[i] = exp(-(normalization_factors[i] - seq_sum));
        }
    }
    """
 })

  c_fw_code = """
    // inputs:  am_scores, batch_idxs, edges, weights, length_models, start_end_states, index, am_score_scales, epoch
    // outputs: output, normalization_factors, posterior_weigths
    assert(n_inputs  == 9);
    assert(n_outputs == 3);
    Ndarray* ary_am_scores         = inputs[0];
    Ndarray* ary_batch_idxs        = inputs[1];
    Ndarray* ary_edges             = inputs[2];
    Ndarray* ary_weights           = inputs[3];
    Ndarray* ary_start_end_states  = inputs[4];
    Ndarray* ary_length_models     = inputs[5];
    Ndarray* ary_index             = inputs[6];
    Ndarray* ary_am_score_scales   = inputs[7];
    Ndarray* ary_epoch             = inputs[8];
    Ndarray* ary_out               = *outputs[0];
    Ndarray* ary_norm_factors      = *outputs[1];
    Ndarray* ary_posterior_weights = *outputs[2];

    assert(Ndarray_DIMS(ary_edges)[1] == Ndarray_DIMS(ary_weights)[0]);

    static unsigned iter = 0u; // used for debug output

    float*    d_am_scores         = Ndarray_DEV_DATA(ary_am_scores);
    int*      d_batch_idxs        = reinterpret_cast<int*>(Ndarray_DEV_DATA(ary_batch_idxs));
    unsigned* d_from              = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA(ary_edges) + 0 * Ndarray_STRIDE(ary_edges, 0));
    unsigned* d_to                = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA(ary_edges) + 1 * Ndarray_STRIDE(ary_edges, 0));
    unsigned* d_emission_idxs     = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA(ary_edges) + 2 * Ndarray_STRIDE(ary_edges, 0));
    unsigned* d_lenmod_idxs       = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA(ary_edges) + 3 * Ndarray_STRIDE(ary_edges, 0));
    unsigned* d_sequence_idxs     = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA(ary_edges) + 4 * Ndarray_STRIDE(ary_edges, 0));
    float*    d_weights           = Ndarray_DEV_DATA(ary_weights);
    float*    d_length_models     = Ndarray_DEV_DATA(ary_length_models);
    unsigned* d_start_states      = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA(ary_start_end_states) + 0 * Ndarray_STRIDE(ary_start_end_states, 0));
    unsigned* d_end_states        = reinterpret_cast<unsigned*>(Ndarray_DEV_DATA(ary_start_end_states) + 1 * Ndarray_STRIDE(ary_start_end_states, 0));
    float*    d_index             = Ndarray_DEV_DATA(ary_index);
    float*    d_am_score_scales   = Ndarray_DEV_DATA(ary_am_score_scales);
    float*    d_epoch             = Ndarray_DEV_DATA(ary_epoch);
    float*    d_out               = Ndarray_DEV_DATA(ary_out);
    float*    d_norm_factors      = Ndarray_DEV_DATA(ary_norm_factors);
    float*    d_posterior_weights = Ndarray_DEV_DATA(ary_posterior_weights);

    const unsigned n_seg_frames      = Ndarray_DIMS(ary_am_scores)[0];
    const unsigned n_batches         = Ndarray_DIMS(ary_am_scores)[1];
    const unsigned n_emissions       = Ndarray_DIMS(ary_am_scores)[2];
    const unsigned n_tot_frames      = Ndarray_DIMS(ary_batch_idxs)[0];
    const unsigned n_seqs            = Ndarray_DIMS(ary_batch_idxs)[1];
    const unsigned n_edges           = Ndarray_DIMS(ary_edges)[1];
    const unsigned n_length_models   = Ndarray_DIMS(ary_length_models)[1];
    const unsigned n_am_score_scales = Ndarray_DIMS(ary_am_score_scales)[0];
    const unsigned n_threads         = 1024u;
    unsigned       n_blocks          = (n_edges + n_threads - 1) / n_threads;

    unsigned tmp;
    HANDLE_ERROR(cudaMemcpy(&tmp, d_end_states + n_seqs - 1, sizeof(float), cudaMemcpyDeviceToHost));

    const unsigned n_states = tmp + 1;

    /*std::cerr << "seg frames: "    << n_seg_frames    << std::endl;
    std::cerr << "batches: "       << n_batches       << std::endl;
    std::cerr << "emissions: "     << n_emissions     << std::endl;
    std::cerr << "tot frames: "    << n_tot_frames    << std::endl;
    std::cerr << "seqs: "          << n_seqs          << std::endl;
    std::cerr << "edges: "         << n_edges         << std::endl;
    std::cerr << "length models: " << n_length_models << std::endl;
    std::cerr << "threads: "       << n_threads       << std::endl;
    std::cerr << "blocks: "        << n_blocks        << std::endl;
    std::cerr << "num states: "    << n_states        << std::endl;*/

    // initialize edge buffer
    const unsigned edge_buffer_size = n_tot_frames * n_seg_frames * n_edges;
    float* d_edge_buffer  = reinterpret_cast<float*>(device_malloc(edge_buffer_size * sizeof(float)));
    HANDLE_LAST_ERROR();
    unsigned n_fill_blocks = (edge_buffer_size + n_threads - 1u) / n_threads;
    fill_array<<<n_fill_blocks, n_threads>>>(d_edge_buffer, std::numeric_limits<float>::infinity(), edge_buffer_size);
    HANDLE_LAST_ERROR();

    // initialize the state buffer
    const unsigned n_ringbuffer_frames = n_seg_frames + 1;
    float* d_state_buffer = reinterpret_cast<float*>(device_malloc(n_states * n_ringbuffer_frames * sizeof(float)));
    HANDLE_LAST_ERROR();
    n_fill_blocks = (n_states * n_ringbuffer_frames + n_threads - 1u) / n_threads;
    fill_array<<<n_fill_blocks, n_threads>>>(d_state_buffer, std::numeric_limits<float>::infinity(), n_states * n_ringbuffer_frames);
    HANDLE_LAST_ERROR();

    // initialize sum buffer and posterior weigths
    n_fill_blocks = (n_batches * n_seg_frames + n_threads - 1u) / n_threads;
    fill_array<<<n_fill_blocks, n_threads>>>(d_norm_factors, 0.0f, n_batches * n_seg_frames);
    HANDLE_LAST_ERROR();
    fill_array<<<n_fill_blocks, n_threads>>>(d_posterior_weights, 0.0f, n_batches * n_seg_frames);
    HANDLE_LAST_ERROR();

    set_start_states<<<1, n_seqs>>>(d_state_buffer, d_start_states);
    HANDLE_LAST_ERROR();

    // fwd pass
    for (unsigned t = 0u; t < n_tot_frames; t++) {
      //std::cerr << "fwd t: " << t << " " << n_tot_frames << std::endl;
      float* d_state_buffer_prev = d_state_buffer + ((t - 1) %% n_ringbuffer_frames) * n_states;
      fill_array<<<n_fill_blocks, n_threads>>>(d_state_buffer_prev, std::numeric_limits<float>::infinity(), n_states);
      HANDLE_LAST_ERROR();
      next_frame_fwd<<<n_blocks, n_threads>>>(t, n_states, n_edges, n_emissions, n_seg_frames, n_tot_frames, n_seqs, n_am_score_scales,
                                              d_sequence_idxs, d_from, d_to, d_weights, d_emission_idxs, d_lenmod_idxs, d_batch_idxs,
                                              d_am_scores, d_length_models, d_am_score_scales, d_epoch,
                                              d_state_buffer, d_edge_buffer + t * n_seg_frames * n_edges);
      HANDLE_LAST_ERROR();

      //std::stringstream ss;
      //ss << "dump/fwd_state_buffer." << t << ".dump";
      //dump_to_file_2d(d_state_buffer, n_ringbuffer_frames, n_states, ss.str());
    }

    //dump_to_file_3d(d_edge_buffer, n_tot_frames, n_seg_frames, n_edges, "dump/fwd_edges.dump");

    // bwd pass
    n_fill_blocks = (n_states * n_ringbuffer_frames + n_threads - 1u) / n_threads;
    fill_array<<<n_fill_blocks, n_threads>>>(d_state_buffer, std::numeric_limits<float>::infinity(), n_states * n_ringbuffer_frames);
    HANDLE_LAST_ERROR();
    n_fill_blocks = (n_states + n_threads - 1u) / n_threads;
    for (unsigned t = n_tot_frames; t > 0; t--) {
      //std::cerr << "bwd t: " << t << " " << n_tot_frames << " buffer next: " << ((t-1) %% n_ringbuffer_frames) << std::endl;
      float* d_state_buffer_next = d_state_buffer + ((t - 1) %% n_ringbuffer_frames) * n_states;
      float* d_state_buffer_prev = d_state_buffer + ( t      %% n_ringbuffer_frames) * n_states;
      fill_array<<<n_fill_blocks, n_threads>>>(d_state_buffer_next, std::numeric_limits<float>::infinity(), n_states);
      HANDLE_LAST_ERROR();
      init_bwd_state_buffer<<<1, n_seqs>>>(t - 1, n_batches, n_seqs, d_batch_idxs, d_index, d_state_buffer_prev, d_end_states);
      HANDLE_LAST_ERROR();
      next_frame_bwd<<<n_blocks, n_threads>>>(t - 1, n_states, n_edges, n_emissions, n_seg_frames, n_tot_frames, n_seqs, n_am_score_scales,
                                              d_sequence_idxs, d_to, d_from, d_weights, d_emission_idxs, d_lenmod_idxs, d_batch_idxs,
                                              d_am_scores, d_length_models, d_am_score_scales, d_epoch,
                                              d_state_buffer, d_edge_buffer + (t - 1) * n_seg_frames * n_edges);
      HANDLE_LAST_ERROR();

      //std::stringstream ss;
      //ss << "dump/bwd_state_buffer." << t << ".dump";
      //dump_to_file_2d(d_state_buffer, n_ringbuffer_frames, n_states, ss.str());
    }

    n_blocks = (n_tot_frames * n_seg_frames + n_threads - 1) / n_threads;
    compute_framewise_sum<<<n_blocks, n_threads, n_threads * n_seqs * sizeof(float)>>>(n_tot_frames, n_seqs, n_seg_frames, n_batches, n_edges,
                                                                                       d_sequence_idxs, d_batch_idxs,
                                                                                       d_index, d_edge_buffer, d_norm_factors);
    HANDLE_LAST_ERROR();

    //dump_to_file_2d(d_norm_factors, n_seg_frames, n_batches, "dump/norm_factors_1.dump");

    if (segmentwise_normalization) {
      n_blocks = (n_batches + n_threads - 1) / n_threads;
      merge_framewise_sum<<<n_blocks, n_threads>>>(n_seg_frames, n_batches, d_index, d_norm_factors);
      HANDLE_LAST_ERROR();
    }

    //dump_to_file_2d(d_norm_factors, n_seg_frames, n_batches, "dump/norm_factors_2.dump");

    n_blocks = (n_tot_frames * n_seqs + n_threads - 1) / n_threads;
    compute_posterior_weights<<<n_blocks, n_threads>>>(n_tot_frames, n_seg_frames, n_seqs, n_batches, d_state_buffer,
                                                       d_start_states, d_batch_idxs, d_index, d_norm_factors, d_posterior_weights);
    HANDLE_LAST_ERROR();

    n_fill_blocks = (n_batches * n_seg_frames * n_emissions + n_threads - 1u) / n_threads;
    fill_array<<<n_fill_blocks, n_threads>>>(d_out, std::numeric_limits<float>::infinity(), n_batches * n_seg_frames * n_emissions);
    HANDLE_LAST_ERROR();

    n_blocks = (n_tot_frames * n_seg_frames * n_edges + n_threads - 1) / n_threads;
    compute_targets<<<n_blocks, n_threads>>>(n_tot_frames, n_seg_frames, n_edges, n_batches, n_seqs, n_emissions,
                                             d_sequence_idxs, d_emission_idxs, d_batch_idxs, d_index, d_edge_buffer, d_norm_factors, d_out);
    HANDLE_LAST_ERROR();

    //dump_to_file_1d(d_weights,       n_edges, "dump/edge_weights.dump");
    //dump_to_file_1d(d_sequence_idxs, n_edges, "dump/sequence_idxs.dump");
    //dump_to_file_2d(d_state_buffer,  n_ringbuffer_frames, n_states,  "dump/state_buffer.dump");
    //dump_to_file_2d(d_batch_idxs,    n_tot_frames,        n_seqs,    "dump/batch_idxs.dump");
    //dump_to_file_2d(d_index,         n_seg_frames,        n_batches, "dump/index.dump");
    //dump_to_file_3d(d_edge_buffer,   n_tot_frames,        n_seg_frames, n_edges,     "dump/edges.dump");
    //dump_to_file_3d(d_am_scores,     n_seg_frames,        n_batches,    n_emissions, "dump/am_scores.dump");
    //dump_to_file_3d(d_out,           n_seg_frames,        n_batches,    n_emissions, "dump/targets.dump");

    if (dump_targets and iter %% dump_targets_interval == 0) {
      std::stringstream ss;
      ss << "dump/targets_" << iter << ".dump";
      dump_to_file_3d(d_out, n_seg_frames, n_batches, n_emissions, ss.str());
      ss.str("");
      ss.clear();
      ss << "dump/norm_factors_" << iter << ".dump";
      dump_to_file_2d(d_norm_factors, n_seg_frames, n_batches, ss.str());
      ss.str("");
      ss.clear();
      ss << "dump/posterior_weights_" << iter << ".dump";
      dump_to_file_2d(d_posterior_weights, n_seg_frames, n_batches, ss.str());
    }

    iter += 1;

    device_free(d_state_buffer);
    device_free(d_edge_buffer);
  """

  cpu_support = False  # TODO: fix CPU support...

  def __init__(self, segmentwise_normalization=False, dump_targets_interval=None):
    to_cpp_bool = lambda v : 'true' if v else 'false';
    extra_lines = []
    extra_lines.append('const bool segmentwise_normalization = %s;' % to_cpp_bool(segmentwise_normalization))
    extra_lines.append('const bool dump_targets = %s;' % to_cpp_bool(dump_targets_interval is not None))
    extra_lines.append('const unsigned dump_targets_interval = %d;' % (0 if dump_targets_interval is None else dump_targets_interval))
    self.c_fw_code = '\n'.join(extra_lines) + '\n' + self.c_fw_code

