import copy
import inspect
import platform
import re
import sys
import unittest

import mock
import numpy as np
import pytest
import six

import chainer
from chainer import backend
from chainer.backends import cuda
from chainer.backends import intel64
import chainer.functions as F
from chainer import initializers
from chainer import testing
from chainer.testing import attr
import chainer.testing.backend
from chainer import variable
import chainerx


if chainerx.is_available():
    import chainerx.testing


class Constant(chainer.Function):

    def __init__(self, outputs):
        self.__outputs = outputs

    def forward_cpu(self, inputs):
        return self.__outputs

    def forward_gpu(self, inputs):
        return tuple(map(cuda.to_gpu, self.__outputs))

    def backward_cpu(self, inputs, grad_outputs):
        return tuple(map(np.zeros_like, inputs))

    def backward_gpu(self, inputs, grad_outputs):
        return tuple(map(cuda.cupy.zeros_like, inputs))


def constant(xs, value):
    return Constant(value)(*xs)


def get_array(xp, arr):
    if xp is np:
        return arr
    if xp is cuda.cupy:
        return cuda.to_gpu(arr)
    if xp is chainerx:
        return chainerx.array(arr)
    assert False


def get_variable(xp, arr):
    return chainer.Variable(get_array(xp, arr))


class MulAdd(chainer.FunctionNode):

    def forward(self, inputs):
        self.retain_inputs((0, 1))
        a, b, c = inputs
        return a * b + c,

    def backward_accumulate(self, target_input_indexes, grad_outputs,
                            grad_inputs):
        a, b = self.get_retained_inputs()
        g, = grad_outputs
        ret = []
        for i, g_in in zip(target_input_indexes, grad_inputs):
            if i == 0:
                ret.append(
                    g * b
                    if g_in is None else
                    muladd(g, b, g_in)
                )
            elif i == 1:
                ret.append(
                    a * g
                    if g_in is None else
                    muladd(a, g, g_in)
                )
            elif i == 2:
                ret.append(
                    g
                    if g_in is None else
                    g + g_in
                )
            else:
                assert False
        return tuple(ret)


def muladd(a, b, c):
    return MulAdd().apply((a, b, c))[0]


@testing.parameterize(*(
    testing.product({
        'var_mapping': [(0, 1, 2)],  # distinct
        'in0_isvar_hasgrad': [(False, False), (True, False), (True, True)],
        'in1_isvar_hasgrad': [(False, False), (True, False), (True, True)],
        'in2_isvar_hasgrad': [(False, False), (True, False), (True, True)],
    }) + testing.product({
        'var_mapping': [
            (0, 0, 1),  # a == b != c
            (0, 1, 0),
            (0, 1, 1),
        ],
        'in0_isvar_hasgrad': [(False, False), (True, False), (True, True)],
        'in1_isvar_hasgrad': [(False, False), (True, False), (True, True)],
    }) + testing.product({
        'var_mapping': [(0, 0, 0)],  # a == b == c
        'in0_isvar_hasgrad': [(False, False), (True, False), (True, True)],
    })
))
class TestBackwardAccumulate(unittest.TestCase):

    shape = 3,

    def setUp(self):
        n = max(self.var_mapping) + 1
        self.inputs_isvar_hasgrad = [
            getattr(self, 'in{}_isvar_hasgrad'.format(i))
            for i in range(n)]

        shape = self.shape
        self.inputs_data = [
            np.random.randn(*shape).astype(np.float32)
            for _ in range(n)]
        self.inputs_grad = [
            np.random.randn(*shape).astype(np.float32) if hasgrad else None
            for _, hasgrad in self.inputs_isvar_hasgrad]
        self.gy = np.random.randn(*shape).astype(np.float32)

    def _get_inputs(self):
        copied_data = [x.copy() for x in self.inputs_data]
        copied_grad = [
            None if g is None else g.copy() for g in self.inputs_data]
        return [
            chainer.Variable(x, grad=g) if isvar else x
            for x, g, (isvar, _) in zip(
                copied_data,
                copied_grad,
                self.inputs_isvar_hasgrad
            )
        ]

    def check_backward_accumulate(self, xp):
        inputs = self._get_inputs()
        a, b, c = [inputs[i] for i in self.var_mapping]
        y = muladd(a, b, c)
        y.grad = self.gy
        y.backward()

        inputs2 = self._get_inputs()
        a2, b2, c2 = [inputs2[i] for i in self.var_mapping]
        y2 = chainer.as_variable(a2 * b2 + c2)
        y2.grad = self.gy
        y2.backward()

        tol = {'atol': 1e-4, 'rtol': 1e-4}
        for x, x2, (isvar, _) in zip(
                inputs, inputs2, self.inputs_isvar_hasgrad):
            if isvar:
                xp.testing.assert_allclose(x.grad, x2.grad, **tol)

    def test_backward_accumulate_cpu(self):
        self.check_backward_accumulate(np)

    def _to_gpu(self):
        self.inputs_data = [cuda.to_gpu(x) for x in self.inputs_data]
        self.inputs_grad = [
            None if g is None else cuda.to_gpu(g)
            for g in self.inputs_grad]
        self.gy = cuda.to_gpu(self.gy)

    @attr.gpu
    def test_backward_accumulate_gpu(self):
        self._to_gpu()
        self.check_backward_accumulate(cuda.cupy)


class TestVariableNode(unittest.TestCase):

    def test_grad(self):
        with pytest.raises(ValueError):
            variable.VariableNode(chainer.Variable(), '', grad=None)


@testing.parameterize(
    {'x_shape': (10,), 'c_shape': (2, 5), 'label': '(2, 5), float32'},
    {'x_shape': (), 'c_shape': (1,), 'label': '(1), float32'},
)
class TestVariable(unittest.TestCase):

    def setUp(self):
        self.x = np.random.uniform(-1, 1, self.x_shape).astype(np.float32)
        self.a = np.random.uniform(0.1, 10, self.x_shape).astype(np.float32)
        self.size = int(np.prod(self.x_shape))
        self.c = np.arange(self.size).reshape(self.c_shape).astype(np.float32)

    @attr.chainerx
    def test_chainerx_init(self):
        a = chainerx.asarray(self.x)
        x = chainer.Variable(a)
        chainerx.testing.assert_array_equal(x.array, a)

    def check_attributes(self, xp):
        a = get_array(xp, self.x)
        x = chainer.Variable(a)
        xp.testing.assert_array_equal(x.array, a)
        assert x.array is x.data
        assert x.shape == self.x.shape
        assert x.ndim == self.x.ndim
        assert x.size == self.x.size
        assert x.dtype == self.x.dtype
        assert x.requires_grad

    @attr.chainerx
    def test_attributes_chainerx(self):
        self.check_attributes(chainerx)

    def test_attributes_cpu(self):
        self.check_attributes(np)

    @attr.gpu
    def test_attributes_gpu(self):
        self.check_attributes(cuda.cupy)

    def test_uninitialized(self):
        a = chainer.Variable(None)
        assert a.xp is np

    def check_grad(self, xp, x, g):
        v = chainer.Variable(x)
        v.grad = g
        xp.testing.assert_array_equal(v.grad, g)

    def test_grad_cpu(self):
        self.check_grad(np, self.x, self.a)

    @attr.gpu
    def test_grad_gpu(self):
        self.check_grad(cuda.cupy, cuda.to_gpu(self.x), cuda.to_gpu(self.a))

    @attr.chainerx
    def test_grad_chainerx(self):
        self.check_grad(
            chainerx, chainerx.array(self.x), chainerx.array(self.a))

    def check_grad_var(self, xp, x, g):
        v = chainer.Variable(x)
        gv = chainer.Variable(g)
        v.grad_var = gv
        xp.testing.assert_array_equal(v.grad, g)

        # Same instance should be returned each time.
        assert v.grad_var is gv

    def test_grad_var_cpu(self):
        self.check_grad_var(np, self.x, self.a)

    @attr.gpu
    def test_grad_var_gpu(self):
        self.check_grad_var(
            cuda.cupy, cuda.to_gpu(self.x), cuda.to_gpu(self.a))

    @attr.chainerx
    def test_grad_var_chainerx(self):
        self.check_grad_var(
            chainerx, chainerx.array(self.x), chainerx.array(self.a))

    def check_len(self, a):
        x = chainer.Variable(a)
        if x.ndim == 0:
            pytest.raises(TypeError, x.__len__)
        else:
            assert len(x) == self.x_shape[0]

    def test_len_cpu(self):
        self.check_len(self.x)

    @attr.gpu
    def test_len_gpu(self):
        self.check_len(cuda.to_gpu(self.x))

    @attr.chainerx
    def test_len_chainerx(self):
        self.check_len(chainerx.array(self.x))

    def check_get_item(self, a):
        x = chainer.Variable(a)
        if len(self.x_shape) > 0:
            slices = slice(2, 5)
            np.testing.assert_equal(backend.CpuDevice().send(x[slices].data),
                                    backend.CpuDevice().send(self.x[slices]))
            slices = slice(2, 5),
            np.testing.assert_equal(backend.CpuDevice().send(x[slices].data),
                                    backend.CpuDevice().send(self.x[slices]))

    def test_get_item_cpu(self):
        self.check_get_item(self.x)

    @attr.gpu
    def test_get_item_gpu(self):
        self.check_get_item(cuda.to_gpu(self.x))

    def check_label(self, expected, c):
        c = chainer.Variable(c)
        assert c.label == expected

    def test_label_cpu(self):
        self.check_label(self.label, self.c)

    @attr.gpu
    def test_label_gpu(self):
        self.check_label(self.label, cuda.to_gpu(self.c))

    def check_backward(self, inputs, intermediates, outputs, retain_grad):
        for o in outputs:
            o.backward(retain_grad)

        assert all([x.grad_var is not None for x in inputs])
        if retain_grad:
            assert all([x.grad_var is not None for x in intermediates])
        else:
            assert all([x.grad_var is None for x in intermediates])
        assert any([x.grad_var is not None for x in outputs])

    # length is number of edges. So, # of Variables created is length+1
    def create_linear_chain(self, length, xp):
        x = get_variable(xp, self.x)
        ret = [x]
        for i in six.moves.range(length):
            ret.append(constant((ret[i], ), (self.a, )))
        if xp is cuda.cupy:
            ret[-1].grad = cuda.cupy.zeros_like(ret[-1].data)
        elif xp is np:
            ret[-1].grad = np.zeros_like(ret[-1].data)
        else:
            assert False
        return ret

    def test_backward_cpu(self):
        ret = self.create_linear_chain(2, np)
        self.check_backward((ret[0], ), (ret[1], ), (ret[2], ), False)

    @attr.gpu
    def test_backward_gpu(self):
        ret = self.create_linear_chain(2, np)
        self.check_backward((ret[0], ), (ret[1], ), (ret[2], ), False)

    @attr.chainerx
    def test_backward_chainerx(self):
        ret = self.create_linear_chain(2, np)
        self.check_backward((ret[0], ), (ret[1], ), (ret[2], ), False)

    def check_backward_accumulate(self, xp):
        x = get_variable(xp, self.x)
        y = x * x
        y.grad = xp.zeros_like(y.data)
        y.backward()
        assert x.grad_var.shape == self.x_shape

    def test_backward_accumulate_cpu(self):
        self.check_backward_accumulate(np)

    @attr.gpu
    def test_backward_accumulate_gpu(self):
        self.check_backward_accumulate(cuda.cupy)

    @attr.chainerx
    def test_backward_accumulate_chainerx(self):
        self.check_backward_accumulate(chainerx)

    def test_backward_cpu_retain_grad(self):
        ret = self.create_linear_chain(2, np)
        self.check_backward((ret[0], ), (ret[1], ), (ret[2], ), True)

    @attr.gpu
    def test_backward_gpu_retain_grad(self):
        ret = self.create_linear_chain(2, cuda.cupy)
        self.check_backward((ret[0], ), (ret[1], ), (ret[2], ), True)

    def check_double_backprop(self, xp):
        x = get_variable(xp, self.x)
        x.grad_var = None

        y = x * x * x
        y.grad = xp.ones_like(y.data)
        y.backward(enable_double_backprop=True)
        gx = x.grad_var
        x.grad_var = None  # clear grad
        gx.grad = xp.ones_like(x.data)
        gx.backward()

        expect = 6 * x
        testing.assert_allclose(x.grad_var.data, expect.data)

    def test_double_backprop_cpu(self):
        self.check_double_backprop(np)

    @attr.gpu
    def test_double_backprop_gpu(self):
        self.check_double_backprop(cuda.cupy)

    @attr.chainerx
    def test_double_backprop_chainerx(self):
        self.check_double_backprop(chainerx)

    def test_backward_no_grad_required(self):
        class DummyId(chainer.functions.math.identity.Identity):

            def backward(self, a, b):
                raise Exception('backward should not be called on inputs that '
                                'do not require grads')

        x = chainer.Variable(self.x)
        y1, y2 = DummyId().apply((x, x))
        x.node._requires_grad = False
        y1.backward()

    def test_unchain(self):
        ret = self.create_linear_chain(3, np)
        old_rank = ret[1].rank
        ret[1].unchain()
        assert ret[1].creator is None
        assert ret[1].rank == old_rank
        self.check_backward((ret[1],), (ret[2],), (ret[3],), False)

    def check_set_none_to_creator(self, use_creator_node):
        ret = self.create_linear_chain(3, np)
        old_rank = ret[1].rank
        if use_creator_node:
            ret[1].creator_node = None
        else:
            ret[1].creator = None
        assert ret[1].creator is None
        assert ret[1].creator_node is None
        assert ret[1].rank == old_rank
        self.check_backward((ret[1],), (ret[2],), (ret[3],), False)

    def test_set_none_to_creator(self):
        self.check_set_none_to_creator(False)

    def test_set_none_to_creator_node(self):
        self.check_set_none_to_creator(True)

    def test_set_none_and_original_to_creator(self):
        ret = self.create_linear_chain(2, np)
        old_rank = ret[1].rank
        creator_node = ret[1].creator_node
        ret[1].creator = None
        assert ret[1].creator is None
        assert ret[1].rank == old_rank

        ret[1].node._rank = -1
        ret[1].creator_node = creator_node
        assert ret[1].creator_node is creator_node
        assert ret[1].rank == creator_node.rank + 1
        self.check_backward((ret[0],), (ret[1],), (ret[2],), False)

    def test_set_fresh_creator(self):
        v = chainer.Variable()
        f = chainer.Function()
        v.creator = f
        assert v.creator is f
        assert v.creator_node is f.node
        assert v.rank == 1

    def test_set_fresh_creator_node(self):
        v = chainer.Variable()
        f = chainer.FunctionNode()
        v.creator_node = f
        assert v.creator is f
        assert v.creator_node is f
        assert v.rank == 1

    def test_unchain_backward_cpu(self):
        ret = self.create_linear_chain(3, np)
        ret[1].unchain_backward()
        self.check_backward((ret[1], ), (ret[2], ), (ret[3], ), False)

    @attr.gpu
    def test_unchain_backward_gpu(self):
        ret = self.create_linear_chain(3, cuda.cupy)
        ret[1].unchain_backward()
        self.check_backward((ret[1], ), (ret[2], ), (ret[3], ), False)

    def test_unchain_backward_cpu_retain_grad(self):
        ret = self.create_linear_chain(3, np)
        ret[1].unchain_backward()
        self.check_backward((ret[1], ), (ret[2], ), (ret[3], ), False)

    @attr.gpu
    def test_unchain_backward_gpu_retain_grad(self):
        ret = self.create_linear_chain(3, np)
        ret[1].unchain_backward()
        self.check_backward((ret[1], ), (ret[2], ), (ret[3], ), False)

    def test_invalid_value_type(self):
        with six.assertRaisesRegex(self, TypeError, 'int'):
            chainer.Variable(1)

    def test_grad_type_check_pass(self):
        a = chainer.Variable(np.empty((3,), dtype=np.float32))
        a.grad = np.ndarray((3,), dtype=np.float32)

    def test_grad_type_check_pass_type(self):
        a = chainer.Variable(np.empty((), dtype=np.float32))
        with pytest.raises(TypeError):
            a.grad = np.float32()

    @attr.gpu
    def test_grad_type_check_type_cpu_gpu_mixture(self):
        a = chainer.Variable(np.empty((3,), dtype=np.float32))
        with pytest.raises(TypeError):
            a.grad = cuda.cupy.empty((3,), dtype=np.float32)

    def test_grad_type_check_dtype(self):
        a = chainer.Variable(np.empty((3,), dtype=np.float32))
        with pytest.raises(TypeError):
            a.grad = np.empty((3,), dtype=np.float64)

    def test_grad_type_check_shape(self):
        a = chainer.Variable(np.empty((3,), dtype=np.float32))
        with pytest.raises(ValueError):
            a.grad = np.empty((2,), dtype=np.float32)

    def check_cleargrad(self, a_data, fill=False):
        xp = backend.get_array_module(a_data)
        a = chainer.Variable(a_data)
        if fill:
            a.grad = xp.full_like(a_data, np.nan)

        a.cleargrad()
        assert a.grad is None

    def test_cleargrad_cpu(self):
        self.check_cleargrad(np.empty(3, dtype=np.float32))

    def test_cleargrad_fill_cpu(self):
        self.check_cleargrad(np.empty(3, dtype=np.float32), fill=True)

    @attr.gpu
    def test_cleargrad_gpu(self):
        self.check_cleargrad(cuda.cupy.empty(3, dtype=np.float32))

    @attr.gpu
    def test_cleargrad_fill_gpu(self):
        self.check_cleargrad(cuda.cupy.empty(3, dtype=np.float32), fill=True)

    @attr.chainerx
    def test_cleargrad_chainerx(self):
        # TODO(hvy): Simplify to chainerx.empty(int, ...) when supported.
        self.check_cleargrad(chainerx.empty((3,), dtype=np.float32))

    @attr.chainerx
    def test_cleargrad_fill_chainerx(self):
        # TODO(hvy): Simplify to chainerx.empty(int, ...) when supported.
        self.check_cleargrad(chainerx.empty((3,), dtype=np.float32), fill=True)

    def check_zerograd(self, a_data, fill=False, grad_var_requires_grad=True,
                       expect_error=False):
        xp = backend.get_array_module(a_data)
        a = chainer.Variable(a_data)
        if fill:
            a.grad_var = chainer.Variable(xp.full_like(a_data, np.nan),
                                          requires_grad=grad_var_requires_grad)
            if xp is not chainerx:
                a.grad_var.creator_node = chainer.FunctionNode()

        with testing.assert_warns(DeprecationWarning):
            if expect_error:
                with pytest.raises(Exception):
                    a.zerograd()
                return
            a.zerograd()

        assert a.grad is not None
        if fill and xp is not chainerx:
            assert a.grad_var.creator_node is None
        xp.testing.assert_array_equal(a.grad, xp.zeros_like(a.grad))

    def test_zerograd_cpu(self):
        self.check_zerograd(np.empty(3, dtype=np.float32))

    def test_zerograd_fill_cpu(self):
        self.check_zerograd(np.empty(3, dtype=np.float32), fill=True)

    @attr.multi_gpu(2)
    def test_zerograds_multi_gpu(self):
        cupy = cuda.cupy
        with cuda.get_device_from_id(1):
            a = chainer.Variable(cupy.empty(3, dtype=np.float32))
        with testing.assert_warns(DeprecationWarning):
            a.zerograd()
        assert a.grad is not None
        assert int(a.grad.device) == 1
        with cuda.get_device_from_id(1):
            g_expect = cupy.zeros_like(a.data)
            cupy.testing.assert_array_equal(a.grad, g_expect)

    @attr.multi_gpu(2)
    def test_zerograds_fill_multi_gpu(self):
        cupy = cuda.cupy
        with cuda.get_device_from_id(1):
            a = chainer.Variable(cupy.empty(3, dtype=np.float32))
            a.grad = cupy.empty_like(a.data)
        with testing.assert_warns(DeprecationWarning):
            a.zerograd()
        assert int(a.grad.device) == 1
        with cuda.get_device_from_id(1):
            g_expect = cupy.zeros_like(a.data)
            cupy.testing.assert_array_equal(a.grad, g_expect)

    @attr.gpu
    def test_zerograd_gpu(self):
        self.check_zerograd(cuda.cupy.empty(3, dtype=np.float32))

    @attr.gpu
    def test_zerograd_fill_gpu(self):
        self.check_zerograd(cuda.cupy.empty(3, dtype=np.float32), fill=True)

    @attr.chainerx
    def test_zerograd_chainerx(self):
        # TODO(hvy): Simplify to chainerx.empty(int, ...) when supported.
        self.check_zerograd(chainerx.empty((3,), dtype=np.float32))

    @attr.chainerx
    def test_zerograd_fill_chainerx(self):
        # TODO(hvy): Simplify to chainerx.empty(int, ...) when supported.
        self.check_zerograd(chainerx.empty((3,), dtype=np.float32), fill=True,
                            grad_var_requires_grad=False)

    @attr.chainerx
    def test_zerograd_fill_chainerx_requiring_grad(self):
        # TODO(hvy): Simplify to chainerx.empty(int, ...) when supported.
        self.check_zerograd(chainerx.empty((3,), dtype=np.float32),
                            fill=True, grad_var_requires_grad=True,
                            expect_error=True)

    def check_copydata(self, data1, data2, expect):
        xp = backend.get_array_module(data1)
        v = chainer.Variable(data1)
        w = chainer.Variable(data2)
        v.copydata(w)
        xp.testing.assert_array_equal(v.data, expect)

    def test_copydata_cpu_to_cpu(self):
        self.check_copydata(np.zeros(3, dtype=np.float32),
                            np.ones(3, dtype=np.float32),
                            np.ones(3, dtype=np.float32))

    @attr.gpu
    def test_copydata_cpu_to_gpu(self):
        cp = cuda.cupy
        self.check_copydata(cp.zeros(3, dtype=np.float32),
                            np.ones(3, dtype=np.float32),
                            cp.ones(3, dtype=np.float32))

    @attr.ideep
    def test_copydata_cpu_to_ideep(self):
        self.check_copydata(intel64.ideep.array(np.zeros(3, dtype=np.float32)),
                            np.ones(3, dtype=np.float32),
                            np.ones(3, dtype=np.float32))

    @attr.gpu
    def test_copydata_gpu_to_gpu(self):
        cp = cuda.cupy
        self.check_copydata(cp.zeros(3, dtype=np.float32),
                            cp.ones(3, dtype=np.float32),
                            cp.ones(3, dtype=np.float32))

    @attr.gpu
    def test_copydata_gpu_to_cpu(self):
        cp = cuda.cupy
        self.check_copydata(np.zeros(3, dtype=np.float32),
                            cp.ones(3, dtype=np.float32),
                            np.ones(3, dtype=np.float32))

    @attr.ideep
    def test_copydata_ideep_to_cpu(self):
        self.check_copydata(np.zeros(3, dtype=np.float32),
                            intel64.ideep.array(np.ones(3, dtype=np.float32)),
                            np.ones(3, dtype=np.float32))

    @attr.multi_gpu(2)
    def test_copydata_gpu_to_another_gpu(self):
        cp = cuda.cupy
        with cuda.get_device_from_id(0):
            data1 = cp.zeros(3, dtype=np.float32)
            expect = cp.ones(3, dtype=np.float32)
        with cuda.get_device_from_id(1):
            data2 = cp.ones(3, dtype=np.float32)
        self.check_copydata(data1, data2, expect)

    def check_addgrad(self, src, dst, expect,
                      clear_src_grad=False, clear_dst_grad=False):
        xp = backend.get_array_module(dst)
        a = chainer.Variable(src)
        a.grad = src
        b = chainer.Variable(dst)
        b.grad = dst
        if clear_src_grad:
            a.cleargrad()
        if clear_dst_grad:
            b.cleargrad()
        b.addgrad(a)
        xp.testing.assert_array_equal(b.grad, expect)
        assert cuda.get_device_from_array(b.data) \
            == cuda.get_device_from_array(b.grad)

    def test_addgrad_cpu_to_cpu(self):
        self.check_addgrad(np.full(3, 10, dtype=np.float32),
                           np.full(3, 20, dtype=np.float32),
                           np.full(3, 30, dtype=np.float32))

    @attr.gpu
    def test_addgrad_cpu_to_gpu(self):
        cp = cuda.cupy
        self.check_addgrad(np.full(3, 10, dtype=np.float32),
                           cp.full(3, 20, dtype=np.float32),
                           cp.full(3, 30, dtype=np.float32))

    @attr.gpu
    def test_addgrad_gpu_to_gpu(self):
        cp = cuda.cupy
        self.check_addgrad(cp.full(3, 10, dtype=np.float32),
                           cp.full(3, 20, dtype=np.float32),
                           cp.full(3, 30, dtype=np.float32))

    @attr.gpu
    def test_addgrad_gpu_to_cpu(self):
        cp = cuda.cupy
        self.check_addgrad(cp.full(3, 10, dtype=np.float32),
                           np.full(3, 20, dtype=np.float32),
                           np.full(3, 30, dtype=np.float32))

    @attr.multi_gpu(2)
    def test_addgrad_gpu_to_gpu_multi(self):
        cp = cuda.cupy
        with cuda.get_device_from_id(1):
            a = cp.full(3, 10, dtype=np.float32)
            b = cp.full(3, 20, dtype=np.float32)
            c = cp.full(3, 30, dtype=np.float32)
        with cuda.get_device_from_id(0):
            self.check_addgrad(a, b, c)

    @attr.multi_gpu(2)
    def test_addgrad_gpu_to_another_gpu(self):
        cp = cuda.cupy
        with cuda.get_device_from_id(1):
            a = cp.full(3, 10, dtype=np.float32)
        with cuda.get_device_from_id(0):
            b = cp.full(3, 20, dtype=np.float32)
            c = cp.full(3, 30, dtype=np.float32)
        self.check_addgrad(a, b, c)

    def test_addgrad_cpu_to_cpu_none_src(self):
        self.check_addgrad(np.full(3, 10, dtype=np.float32),
                           np.full(3, 20, dtype=np.float32),
                           np.full(3, 20, dtype=np.float32),
                           clear_src_grad=True)

    @attr.gpu
    def test_addgrad_gpu_to_gpu_none_src(self):
        cp = cuda.cupy
        self.check_addgrad(cp.full(3, 10, dtype=np.float32),
                           cp.full(3, 20, dtype=np.float32),
                           cp.full(3, 20, dtype=np.float32),
                           clear_src_grad=True)

    @attr.multi_gpu(2)
    def test_addgrad_gpu_to_another_gpu_none_src_dev0(self):
        cp = cuda.cupy
        with cuda.get_device_from_id(1):
            a = cp.full(3, 10, dtype=np.float32)
        with cuda.get_device_from_id(0):
            b = cp.full(3, 20, dtype=np.float32)
            c = cp.full(3, 20, dtype=np.float32)
        with cuda.get_device_from_id(0):
            self.check_addgrad(a, b, c, clear_src_grad=True)

    @attr.multi_gpu(2)
    def test_addgrad_gpu_to_another_gpu_none_src_dev1(self):
        cp = cuda.cupy
        with cuda.get_device_from_id(1):
            a = cp.full(3, 10, dtype=np.float32)
        with cuda.get_device_from_id(0):
            b = cp.full(3, 20, dtype=np.float32)
            c = cp.full(3, 20, dtype=np.float32)
        with cuda.get_device_from_id(1):
            self.check_addgrad(a, b, c, clear_src_grad=True)

    def test_addgrad_cpu_to_cpu_none_dst(self):
        self.check_addgrad(np.full(3, 20, dtype=np.float32),
                           np.full(3, 10, dtype=np.float32),
                           np.full(3, 20, dtype=np.float32),
                           clear_dst_grad=True)

    @attr.gpu
    def test_addgrad_gpu_to_gpu_none_dst(self):
        cp = cuda.cupy
        self.check_addgrad(cp.full(3, 20, dtype=np.float32),
                           cp.full(3, 10, dtype=np.float32),
                           cp.full(3, 20, dtype=np.float32),
                           clear_dst_grad=True)

    @attr.multi_gpu(2)
    def test_addgrad_gpu_to_another_gpu_none_dst_dev0(self):
        cp = cuda.cupy
        with cuda.get_device_from_id(1):
            a = cp.full(3, 20, dtype=np.float32)
        with cuda.get_device_from_id(0):
            b = cp.full(3, 10, dtype=np.float32)
            c = cp.full(3, 20, dtype=np.float32)
        with cuda.get_device_from_id(0):
            self.check_addgrad(a, b, c, clear_dst_grad=True)

    @attr.multi_gpu(2)
    def test_addgrad_gpu_to_another_gpu_none_dst_dev1(self):
        cp = cuda.cupy
        with cuda.get_device_from_id(1):
            a = cp.full(3, 20, dtype=np.float32)
        with cuda.get_device_from_id(0):
            b = cp.full(3, 10, dtype=np.float32)
            c = cp.full(3, 20, dtype=np.float32)
        with cuda.get_device_from_id(1):
            self.check_addgrad(a, b, c, clear_dst_grad=True)

    def test_addgrad_none_src_dst(self):
        x = chainer.Variable(self.x)
        y = chainer.Variable(self.x)
        y.addgrad(x)
        assert y.grad is None

    def test_pickle_cpu(self):
        x = chainer.Variable(self.x)
        x.grad = np.ones_like(x.data)
        binary = six.moves.cPickle.dumps(x)
        d = six.moves.cPickle.loads(binary)
        np.testing.assert_array_equal(x.data, d.data)
        np.testing.assert_array_equal(x.grad, d.grad)

    @attr.gpu
    def test_pickle_gpu(self):
        cp = cuda.cupy
        x = chainer.Variable(self.x)
        x.grad = np.ones_like(x.data)
        x.to_gpu()
        binary = six.moves.cPickle.dumps(x)
        d = six.moves.cPickle.loads(binary)
        cp.testing.assert_array_equal(x.data, d.data)
        cp.testing.assert_array_equal(x.grad, d.grad)


@testing.parameterize(
    {'array_require_grad': False, 'requires_grad': 'default',
     'expected': True},
    {'array_require_grad': False, 'requires_grad': False, 'expected': False},
    {'array_require_grad': False, 'requires_grad': True, 'expected': True},
    {'array_require_grad': True, 'requires_grad': 'default',
     'expected': True},
    {'array_require_grad': True, 'requires_grad': False, 'expected': 'raise'},
    {'array_require_grad': True, 'requires_grad': True, 'expected': True},
)
@attr.chainerx
class TestVariableChainerXInitRequiresGrad(unittest.TestCase):

    def test_chainerx_init_requires_grad(self):
        x = chainerx.ones((2,), dtype=np.float32)
        if self.array_require_grad:
            x.require_grad()

        def v():
            if self.requires_grad == 'default':
                return chainer.Variable(x)
            else:
                return chainer.Variable(x, requires_grad=self.requires_grad)

        if self.expected == 'raise':
            with pytest.raises(ValueError):
                v()
        else:
            assert v().requires_grad is self.expected


@testing.parameterize(
    {'x_shape': (10,)},
    {'x_shape': ()},
)
class TestVariableToCpu(unittest.TestCase):

    def setUp(self):
        self.x = np.zeros(self.x_shape, dtype=np.float32)
        self.gx = np.ones_like(self.x)

    def check_to_cpu(self, x, gx, requires_grad=True):
        x_var = chainer.Variable(x, requires_grad=requires_grad)

        set_grad_var = requires_grad or not isinstance(x, chainerx.ndarray)
        if set_grad_var:
            x_var.grad_var = chainer.Variable(gx, requires_grad=requires_grad)

        x_var.to_cpu()

        assert x_var.xp is np
        assert x_var.node is not None
        assert isinstance(x_var.data, np.ndarray)
        assert x.shape == x_var.shape
        assert x.dtype == x_var.dtype
        np.testing.assert_array_equal(
            backend.CpuDevice().send(x_var.data), backend.CpuDevice().send(x))

        if set_grad_var:
            assert isinstance(x_var.grad, np.ndarray)
            assert gx.shape == x_var.grad.shape
            assert gx.dtype == x_var.grad.dtype
            np.testing.assert_array_equal(
                backend.CpuDevice().send(x_var.grad),
                backend.CpuDevice().send(gx))
            assert x_var.grad_var is not None
            assert x_var.grad_var.node is not None
        else:
            assert x_var.grad is None
            assert x_var.grad_var is None

        orig_xp = backend.get_array_module(x, gx)
        if orig_xp is np:
            assert x_var.data is x
            assert x_var.grad is gx
        else:
            assert x_var.data is not x
            assert not set_grad_var or x_var.grad is not gx

        assert x_var.xp is not chainerx

    def test_to_cpu_from_cpu(self):
        self.check_to_cpu(self.x, self.gx)

    @attr.gpu
    def test_to_cpu_from_gpu(self):
        self.check_to_cpu(cuda.to_gpu(self.x), cuda.to_gpu(self.gx))

    @attr.chainerx
    def test_to_cpu_from_chainerx(self):
        self.check_to_cpu(
            chainerx.array(self.x),
            chainerx.array(self.gx),
            requires_grad=False)

    @attr.chainerx
    def test_to_cpu_from_chainerx_requiring_grad(self):
        with self.assertRaises(RuntimeError):
            self.check_to_cpu(
                chainerx.array(self.x),
                chainerx.array(self.gx),
                requires_grad=True)


@testing.parameterize(
    {'x_shape': (10,)},
    {'x_shape': ()},
)
@attr.gpu
class TestVariableToGpu(unittest.TestCase):

    def setUp(self):
        self.x = np.zeros(self.x_shape, dtype=np.float32)
        self.gx = np.ones_like(self.x)

    def check_to_gpu(self, x, gx, device=None, requires_grad=True):
        x_var = chainer.Variable(x, requires_grad=requires_grad)

        set_grad_var = requires_grad or not isinstance(x, chainerx.ndarray)
        if set_grad_var:
            x_var.grad_var = chainer.Variable(gx, requires_grad=requires_grad)

        x_var.to_gpu(device)

        assert x_var.xp is cuda.cupy
        assert x_var.node is not None
        assert isinstance(x_var.data, cuda.cupy.ndarray)
        assert x.shape == x_var.shape
        assert x.dtype == x_var.dtype
        device = cuda.Device(device)
        assert cuda.get_device_from_array(x_var.data) == device
        np.testing.assert_array_equal(
            backend.CpuDevice().send(x_var.data), backend.CpuDevice().send(x))

        if set_grad_var:
            assert isinstance(x_var.grad, cuda.cupy.ndarray)
            assert gx.shape == x_var.grad.shape
            assert gx.dtype == x_var.grad.dtype
            assert cuda.get_device_from_array(x_var.grad) == device
            np.testing.assert_array_equal(
                backend.CpuDevice().send(x_var.grad),
                backend.CpuDevice().send(gx))
            assert x_var.grad_var is not None
            assert x_var.grad_var.node is not None
        else:
            assert x_var.grad is None
            assert x_var.grad_var is None

        orig_xp = backend.get_array_module(x, gx)
        orig_device = cuda.get_device_from_array(x)
        if orig_xp is cuda.cupy and orig_device == device:
            assert x_var.data is x
            assert x_var.grad is gx
        else:
            assert x_var.data is not x
            assert not set_grad_var or x_var.grad is not gx

        assert x_var.xp is not chainerx

    def test_to_gpu_from_cpu(self):
        self.check_to_gpu(self.x, self.gx)

    def test_to_gpu_from_gpu(self):
        self.check_to_gpu(cuda.to_gpu(self.x), cuda.to_gpu(self.gx))

    @attr.multi_gpu(2)
    def test_to_gpu_from_another_gpu(self):
        self.check_to_gpu(cuda.to_gpu(self.x), cuda.to_gpu(self.gx), 1)

    @attr.chainerx
    def test_to_gpu_from_chainerx(self):
        self.check_to_gpu(
            chainerx.array(self.x),
            chainerx.array(self.gx),
            requires_grad=False)

    @attr.chainerx
    def test_to_gpu_from_chainerx_requiring_grad(self):
        with self.assertRaises(RuntimeError):
            self.check_to_gpu(
                chainerx.array(self.x),
                chainerx.array(self.gx),
                requires_grad=True)


@testing.parameterize(
    {'x_shape': (10,)},
    {'x_shape': ()},
)
@attr.chainerx
class TestVariableToChainerX(unittest.TestCase):

    def setUp(self):
        self.x = np.zeros(self.x_shape, dtype=np.float32)
        self.gx = np.ones_like(self.x)

    def infer_expected_device(self, *arrays):
        xp = backend.get_array_module(*arrays)
        if xp is np:
            return chainerx.get_device('native', 0)
        elif xp is cuda.cupy:
            return chainerx.get_device('cuda', arrays[0].device.id)
        elif xp is chainerx:
            return arrays[0].device
        assert False

    def check_to_chainerx(self, x, gx, requires_grad=True):
        x_var = chainer.Variable(x, requires_grad=requires_grad)
        x_var.grad_var = chainer.Variable(gx, requires_grad=requires_grad)

        x_var.to_chainerx()

        expected_device = self.infer_expected_device(x, gx)

        assert x_var.xp is chainerx
        with pytest.raises(RuntimeError):
            x_var.node
        assert isinstance(x_var.array, chainerx.ndarray)
        assert x.shape == x_var.shape
        assert x.dtype == x_var.dtype
        assert x_var.data.device is expected_device
        np.testing.assert_array_equal(
            backend.CpuDevice().send(x_var.data), backend.CpuDevice().send(x))

        if requires_grad:
            assert isinstance(x_var.grad, chainerx.ndarray)
            assert gx.shape == x_var.grad.shape
            assert gx.dtype == x_var.grad.dtype
            assert x_var.grad.device is expected_device
            np.testing.assert_array_equal(
                backend.CpuDevice().send(x_var.grad),
                backend.CpuDevice().send(gx))
            assert x_var.grad_var is not None
            with pytest.raises(RuntimeError):
                x_var.grad_var.node
        else:
            assert x_var.grad is None
            assert x_var.grad_var is None

        assert x_var.xp is chainerx

    def test_to_chainerx_from_numpy(self):
        self.check_to_chainerx(self.x, self.gx)

    @attr.gpu
    def test_to_chainerx_from_cupy(self):
        self.check_to_chainerx(cuda.to_gpu(self.x), cuda.to_gpu(self.gx))

    # TODO(hvy): Write test when implemented.
    @attr.ideep
    def test_ideep_to_chainerx(self):
        raise unittest.SkipTest('Not yet supported')

    def test_to_chainerx_from_chainerx(self):
        self.check_to_chainerx(
            chainerx.array(self.x), chainerx.array(self.gx))

    def test_to_chainerx_from_another_device(self):
        self.check_to_chainerx(
            chainerx.array(self.x), chainerx.array(self.gx))

    def test_to_chainerx_not_requiring_grad(self):
        self.check_to_chainerx(self.x, self.gx, requires_grad=False)

    def test_to_chainerx_with_creator(self):
        x = chainer.Variable(self.x)
        y = x * x
        with self.assertRaises(RuntimeError):
            y.to_chainerx()


@testing.parameterize(
    {'x_shape': (10,)},
    {'x_shape': ()},
)
@chainer.testing.backend.inject_backend_tests(
    ['test_from_chainerx'],
    [
        # NumPy
        {},
        # CuPy
        {'use_cuda': True, 'cuda_device': 0},
        {'use_cuda': True, 'cuda_device': 1},
        # ChainerX
        {'use_chainerx': True, 'chainerx_device': 'native:0'},
        {'use_chainerx': True, 'chainerx_device': 'cuda:0'},
        {'use_chainerx': True, 'chainerx_device': 'cuda:1'},
    ])
@attr.chainerx
class TestVariableFromChainerX(unittest.TestCase):

    def setUp(self):
        self.x = chainerx.zeros(self.x_shape, dtype=np.float32)

    def infer_expected_xp_and_device(self, x):
        xp = backend.get_array_module(x)
        if xp is np:
            return xp, None
        elif xp is cuda.cupy:
            return xp, x.device
        elif xp is chainerx:
            backend_name = x.device.backend.name
            if backend_name == 'native':
                return np, None
            elif backend_name == 'cuda':
                return cuda.cupy, cuda.cupy.cuda.Device(x.device.index)
        assert False

    def test_from_chainerx(self, backend_config):
        x = backend_config.get_array(self.x)
        x_var = chainer.Variable(x, requires_grad=False)
        x_var.from_chainerx()

        expected_xp, expected_device = self.infer_expected_xp_and_device(x)

        assert x_var.xp is expected_xp
        assert x_var.node is not None
        assert isinstance(x_var.array, expected_xp.ndarray)
        assert expected_device is None or x_var.array.device == expected_device
        assert x.shape == x_var.shape
        assert x.dtype == x_var.dtype
        assert x_var.grad is None
        assert x_var.grad_var is None
        np.testing.assert_array_equal(
            backend.CpuDevice().send(x_var.array), backend.CpuDevice().send(x))

    def test_invalid_from_chainerx_requires_grad(self):
        x = chainer.Variable(self.x, requires_grad=True)
        with self.assertRaises(RuntimeError):
            x.from_chainerx()


@testing.parameterize(
    {'x_shape': (10,)},
    {'x_shape': ()},
)
@attr.chainerx
class TestVariableToDevice(unittest.TestCase):

    def setUp(self):
        self.x = np.zeros(self.x_shape, dtype=np.float32)
        self.gx = np.ones_like(self.x)

    def check_to_device(self, x, gx, device_spec, expected_xp):
        x_var = chainer.Variable(x)
        x_var.grad_var = chainer.Variable(gx)

        x_var.to_device(device_spec)

        assert x_var.xp is expected_xp
        assert x_var.grad_var.xp is expected_xp

    def test_to_device_numpy(self):
        self.check_to_device(self.x, self.gx, np, np)

    @attr.gpu
    def test_to_device_cupy(self):
        self.check_to_device(self.x, self.gx, (cuda.cupy, 0), cuda.cupy)

    @attr.chainerx
    def test_to_device_chainerx(self):
        self.check_to_device(self.x, self.gx, 'native:0', chainerx)


_to_device_twice_backend_params = [
    # NumPy
    {},
    # CuPy
    {'use_cuda': True, 'cuda_device': 0},
    {'use_cuda': True, 'cuda_device': 1},
    # ChainerX
    {'use_chainerx': True, 'chainerx_device': 'native:0'},
    {'use_chainerx': True, 'chainerx_device': 'cuda:0'},
    {'use_chainerx': True, 'chainerx_device': 'cuda:1'},
]


@testing.parameterize(*testing.product(
    {
        'x_shape': [(10,), (), None],
        'requires_grad': [True, False],
    }))
@testing.backend.inject_backend_tests(None, _to_device_twice_backend_params)
@testing.backend.inject_backend_tests(None, _to_device_twice_backend_params)
class TestVariableToDeviceTwice(unittest.TestCase):

    def setUp(self):
        if self.x_shape is None:
            self.x = None
        else:
            self.x = np.zeros(self.x_shape, dtype=np.float32)

    def test_to_device_twice(self, backend_config1, backend_config2):
        device1 = backend_config1.device
        device2 = backend_config2.device
        var = chainer.Variable(self.x, requires_grad=self.requires_grad)

        # Transfer to device 1
        var.to_device(device1)

        # Transfer to device 2
        should_fail = (
            self.requires_grad
            and self.x is not None
            and device1.xp is chainerx
            and device2.xp is not chainerx)
        if should_fail:
            # Non-ChainerX device to ChainerX device should fail if
            # requires_grad
            with pytest.raises(RuntimeError):
                var.to_device(device2)
        else:
            # Should succeed
            var.to_device(device2)

            assert var.requires_grad == self.requires_grad
            if self.x is None:
                assert var.array is None
                assert var.data is None
            else:
                assert isinstance(var.array, device2.xp.ndarray)
                assert backend.get_device_from_array(var.array) == device2
                np.testing.assert_array_equal(
                    self.x,
                    backend.CpuDevice().send(var.array))


class TestVariableBasic(unittest.TestCase):
    def test_unhashable(self):
        a = chainer.Variable(np.ones((2,)))
        with six.assertRaisesRegex(self, TypeError, '^unhashable type: '):
            hash(a)

    def test_unequatable(self):
        a = chainer.Variable(np.ones((2,)))
        b = chainer.Variable(np.ones((2,)))
        with pytest.raises(NotImplementedError):
            a == b
        with pytest.raises(NotImplementedError):
            a == a
        with pytest.raises(NotImplementedError):
            a != b
        with pytest.raises(NotImplementedError):
            a != a

    def test_uncomparable(self):
        a = chainer.Variable(np.ones((2,)))
        b = chainer.Variable(np.ones((2,)))
        with pytest.raises(NotImplementedError):
            a < b
        with pytest.raises(NotImplementedError):
            a <= b
        with pytest.raises(NotImplementedError):
            a > b
        with pytest.raises(NotImplementedError):
            a >= b

    def test_bool_inconvertible(self):
        a = chainer.Variable(np.ones((2,)))
        with pytest.raises(NotImplementedError):
            if a:
                pass
        with pytest.raises(NotImplementedError):
            if not a:
                pass


class TestVariableDataAssign(unittest.TestCase):

    def test_variable_data_assign(self):
        x = chainer.Variable(np.ones((3, 2), np.float32))
        chainer.functions.sin(x)
        x.data = np.ones((2, 4), np.float64)
        assert x.data.shape == (2, 4)
        assert x.data.dtype == np.float64
        assert x.shape == (2, 4)
        assert x.dtype == np.float64
        assert x.node.shape == (2, 4)
        assert x.node.dtype == np.float64
        assert x.node.data.shape == (2, 4)
        assert x.node.data.dtype == np.float64

    @attr.gpu
    def test_to_gpu(self):
        x = chainer.Variable(np.ones((3, 2), np.float32))
        chainer.functions.sin(x)
        x.to_gpu()
        assert x.data is x.node.data
        x.to_cpu()
        assert x.data is x.node.data

    @attr.ideep
    def test_to_intel64(self):
        x = chainer.Variable(np.ones((3, 2), np.float32))
        chainer.functions.sin(x)
        x.to_intel64()
        assert x.data is x.node.data
        x.to_cpu()
        assert x.data is x.node.data


class TestParameter(unittest.TestCase):

    def setUp(self):
        self.a = np.random.rand(3, 2).astype(np.float32)

    def test_initializer(self):
        x = chainer.Parameter(shape=(1,))
        assert x.initializer is not None

    def test_initialize_by_scalar(self):
        x = chainer.Parameter(2., (3,))
        np.testing.assert_array_equal(x.data, np.array([2., 2., 2.]))

    def test_initialize_by_initializer(self):
        x = chainer.Parameter(initializers.One(), (3,))
        np.testing.assert_array_equal(
            x.data, np.array([1., 1., 1.], dtype='f'))

    def test_initialize_by_none(self):
        x = chainer.Parameter(None, (3,))
        np.testing.assert_array_equal(
            x.data, np.full((3,), np.nan, dtype='f'))

    def test_initialize_by_array(self):
        data = np.array([1., 2., 3.], dtype='f')
        x = chainer.Parameter(data)
        assert x.data is data

    @attr.gpu
    def test_initialize_by_cupy_array(self):
        data = cuda.cupy.array([1., 2., 3.], dtype='f')
        x = chainer.Parameter(data, (3,))
        assert isinstance(x.data, cuda.cupy.ndarray)
        cuda.cupy.testing.assert_array_equal(x.data, data)

    @attr.chainerx
    def test_initialize_by_chainerx_array(self):
        data = chainerx.array([1., 2., 3.], dtype='f')
        x = chainer.Parameter(data)
        assert isinstance(x.data, chainerx.ndarray)
        chainerx.testing.assert_array_equal(x.data, data)

    def test_update_rule(self):
        update_rule = mock.MagicMock()
        g = self.a.copy()
        x = chainer.Parameter(self.a)
        x.grad = g
        x.update_rule = update_rule
        x.update()
        assert update_rule.update.call_count == 1
        assert update_rule.update.call_args_list[0] == [(x,), {}]

    def test_update_rule_without_grad(self):
        update_rule = mock.MagicMock()
        x = chainer.Parameter(self.a)
        x.update_rule = update_rule
        x.update()
        assert update_rule.update.call_count == 1


@testing.parameterize(
    {'x_shape': (10,)},
    {'x_shape': ()},
)
class TestParameterToDevice(unittest.TestCase):

    def check_to_device(self, x, device_spec, expected_xp):
        assert isinstance(x, chainer.Parameter)
        x.to_device(device_spec)
        assert x.xp is expected_xp

    def check_initializer(self, shape, device_spec, expected_xp):
        x = chainer.Parameter(shape=shape)
        self.check_to_device(x, device_spec, expected_xp)

    def check_initialize_by_scalar(self, shape, device_spec, expected_xp):
        x = chainer.Parameter(2., shape)
        self.check_to_device(x, device_spec, expected_xp)

    def check_initialize_by_initializer(self, shape, device_spec, expected_xp):
        x = chainer.Parameter(initializers.One(), shape)
        self.check_to_device(x, device_spec, expected_xp)

    def check_initialize_by_none(self, shape, device_spec, expected_xp):
        x = chainer.Parameter(None, shape)
        self.check_to_device(x, device_spec, expected_xp)

    def check_initialize_by_array(self, shape, device_spec, expected_xp):
        data = np.random.uniform(-1, 1, shape).astype('f')
        x = chainer.Parameter(data)
        self.check_to_device(x, device_spec, expected_xp)

    def test_initializer_to_device_numpy(self):
        self.check_initializer(self.x_shape, np, np)

    @attr.gpu
    def test_initializer_to_device_cupy(self):
        self.check_initializer(self.x_shape, (cuda.cupy, 0), cuda.cupy)

    @attr.chainerx
    def test_initializer_to_device_chainerx(self):
        self.check_initializer(self.x_shape, 'native:0', chainerx)

    def test_initialize_by_scalar_to_device_numpy(self):
        self.check_initialize_by_scalar(self.x_shape, np, np)

    @attr.gpu
    def test_initialize_by_scalar_to_device_cupy(self):
        self.check_initialize_by_scalar(
            self.x_shape, (cuda.cupy, 0), cuda.cupy)

    @attr.chainerx
    def test_initialize_by_scalar_to_device_chainerx(self):
        self.check_initialize_by_scalar(self.x_shape, 'native:0', chainerx)

    def test_initialize_by_initializer_to_device_numpy(self):
        self.check_initialize_by_initializer(self.x_shape, np, np)

    @attr.gpu
    def test_initialize_by_initializer_to_device_cupy(self):
        self.check_initialize_by_initializer(
            self.x_shape, (cuda.cupy, 0), cuda.cupy)

    @attr.chainerx
    def test_initialize_by_initializer_to_device_chainerx(self):
        self.check_initialize_by_initializer(
            self.x_shape, 'native:0', chainerx)

    def test_initialize_by_none_to_device_numpy(self):
        self.check_initialize_by_none(self.x_shape, np, np)

    @attr.gpu
    def test_initialize_by_none_to_device_cupy(self):
        self.check_initialize_by_none(self.x_shape, (cuda.cupy, 0), cuda.cupy)

    @attr.chainerx
    def test_initialize_by_none_to_device_chainerx(self):
        self.check_initialize_by_none(self.x_shape, 'native:0', chainerx)

    def test_initialize_by_array_to_device_numpy(self):
        self.check_initialize_by_array(self.x_shape, np, np)

    @attr.gpu
    def test_initialize_by_array_to_device_cupy(self):
        self.check_initialize_by_array(self.x_shape, (cuda.cupy, 0), cuda.cupy)

    @attr.chainerx
    def test_initialize_by_array_to_device_chainerx(self):
        self.check_initialize_by_array(self.x_shape, 'native:0', chainerx)


@testing.parameterize(
    {'x_shape': (10,)},
    {'x_shape': ()},
)
@attr.chainerx
class TestParameterToChainerX(unittest.TestCase):

    def check_to_chainerx(self, x):
        assert isinstance(x, chainer.Parameter)
        x.to_chainerx()
        assert x.xp is chainerx

    def check_initializer(self, shape):
        x = chainer.Parameter(shape=shape)
        self.check_to_chainerx(x)

    def check_initialize_by_scalar(self, shape):
        x = chainer.Parameter(2., shape)
        self.check_to_chainerx(x)

    def check_initialize_by_initializer(self, shape):
        x = chainer.Parameter(initializers.One(), shape)
        self.check_to_chainerx(x)

    def check_initialize_by_none(self, shape):
        x = chainer.Parameter(None, shape)
        self.check_to_chainerx(x)

    def check_initialize_by_array(self, shape, xp, device=None):
        if device is not None:
            data = xp.random.uniform(-1, 1, shape, device=device).astype('f')
        else:
            data = xp.random.uniform(-1, 1, shape).astype('f')

        x = chainer.Parameter(data)
        self.check_to_chainerx(x)

    def test_initializer_to_chainerx(self):
        self.check_initializer(self.x_shape)

    def test_initialize_by_scalar_to_chainerx(self):
        self.check_initialize_by_scalar(self.x_shape)

    def test_initialize_by_initializer_to_chainerx(self):
        self.check_initialize_by_initializer(self.x_shape)

    def test_initialize_by_none_to_chainerx(self):
        self.check_initialize_by_none(self.x_shape)

    def test_initialize_by_array_to_chainerx_numpy(self):
        self.check_initialize_by_array(self.x_shape, np)

    @attr.gpu
    def test_initialize_by_array_to_chainerx_cupy(self):
        self.check_initialize_by_array(self.x_shape, cuda.cupy)

    @attr.chainerx
    def test_initialize_by_array_to_chainerx_chainerx_native(self):
        self.check_initialize_by_array(self.x_shape, chainerx, 'native:0')

    @attr.gpu
    @attr.chainerx
    def test_initialize_by_array_to_chainerx_chainerx_cuda(self):
        self.check_initialize_by_array(self.x_shape, chainerx, 'cuda:0')


@testing.parameterize(
    {'x_shape': (10,)},
    {'x_shape': ()},
)
@attr.chainerx
class TestParameterFromChainerX(unittest.TestCase):

    def check_from_chainerx(self, x, expected_xp):
        assert isinstance(x, chainer.Parameter)
        x.from_chainerx()
        assert x.xp is expected_xp

    def check_initializer(self, shape, expected_xp):
        x = chainer.Parameter(shape=shape)
        self.check_from_chainerx(x, expected_xp)

    def check_initialize_by_scalar(self, shape, expected_xp):
        x = chainer.Parameter(2., shape)
        self.check_from_chainerx(x, expected_xp)

    def check_initialize_by_initializer(self, shape, expected_xp):
        x = chainer.Parameter(initializers.One(), shape)
        self.check_from_chainerx(x, expected_xp)

    def check_initialize_by_none(self, shape, expected_xp):
        x = chainer.Parameter(None, shape)
        self.check_from_chainerx(x, expected_xp)

    def check_initialize_by_array(self, shape, xp, expected_xp, device=None):
        if device is not None:
            data = xp.random.uniform(-1, 1, shape, device=device).astype('f')
        else:
            data = xp.random.uniform(-1, 1, shape).astype('f')

        x = chainer.Parameter(data)
        self.check_from_chainerx(x, expected_xp)

    def test_initializer_from_chainerx(self):
        self.check_initializer(self.x_shape, np)

    def test_initialize_by_scalar_from_chainerx(self):
        self.check_initialize_by_scalar(self.x_shape, np)

    def test_initialize_by_initializer_from_chainerx(self):
        self.check_initialize_by_initializer(self.x_shape, np)

    def test_initialize_by_none_from_chainerx(self):
        self.check_initialize_by_none(self.x_shape, np)

    def test_initialize_by_array_from_chainerx_numpy(self):
        self.check_initialize_by_array(self.x_shape, np, np)

    @attr.gpu
    def test_initialize_by_array_from_chainerx_cupy(self):
        self.check_initialize_by_array(self.x_shape, cuda.cupy, cuda.cupy)

    @attr.chainerx
    def test_initialize_by_array_from_chainerx_chainerx_native(self):
        self.check_initialize_by_array(self.x_shape, chainerx, np, 'native:0')

    @attr.gpu
    @attr.chainerx
    def test_initialize_by_array_from_chainerx_chainerx_cuda(self):
        self.check_initialize_by_array(
            self.x_shape, chainerx, cuda.cupy, 'cuda:0')


class TestUninitializedParameter(unittest.TestCase):

    def setUp(self):
        self.a = np.random.rand(3, 2).astype(np.float32)
        self.b = np.random.rand(*self.a.shape).astype(self.a.dtype)

    def test_init_without_data(self):
        x = chainer.Parameter()
        assert x.data is None
        assert x.grad is None

    def test_initialize(self):
        x = chainer.Parameter()
        x.initialize((3, 2))
        assert x.shape == (3, 2)
        assert x.dtype == np.float32
        np.testing.assert_array_equal(x.data, np.float32('nan'))
        np.testing.assert_array_equal(x.grad, np.float32('nan'))
        assert backend.get_device_from_array(x.data).xp is np
        assert backend.get_device_from_array(x.grad).xp is np

    def check_constant_initialization(self, x, a, xp, expected_device):
        x.initialize(a.shape)
        assert isinstance(x.data, xp.ndarray)
        xp.testing.assert_array_equal(x.data, xp.asarray(a))
        xp.testing.assert_array_equal(x.grad, np.float32('nan'))
        assert backend.get_device_from_array(x.data) == expected_device
        assert backend.get_device_from_array(x.grad) == expected_device

    def test_initialize_with_initializer(self):
        x = chainer.Parameter(initializers.Constant(self.a))
        self.check_constant_initialization(
            x, self.a, np, chainer.get_device(np))

    def test_initialize_dtype(self):
        initializer = initializers.Zero(np.float64)
        x = chainer.Parameter(initializer=initializer)
        x.initialize((2, 3))
        assert x.data.dtype == np.float64
        assert x.grad.dtype == np.float64

    def test_initialize_by_callable_default_dtype(self):
        def initializer(array):
            array.fill(1.0)
        x = chainer.Parameter(initializer=initializer)
        with chainer.using_config('dtype', np.float16):
            x.initialize((3, 2))
        assert x.data.dtype == np.float16
        assert x.grad.dtype == np.float16

    def test_initialize_node(self):
        initializer = initializers.Zero(np.float64)
        x = chainer.Parameter(initializer=initializer)
        x.initialize((2, 3))
        assert x.node.shape == (2, 3)
        assert x.node.dtype == np.float64

    @attr.gpu
    def test_initialize_to_gpu(self):
        x = chainer.Parameter(initializer=initializers.Constant(self.a))
        x.to_gpu()
        self.check_constant_initialization(
            x, self.a, cuda.cupy, chainer.get_device((cuda.cupy, 0)))

    @attr.multi_gpu(2)
    def test_initialize_to_noncurrent_gpu(self):
        x = chainer.Parameter(initializer=initializers.Constant(self.a))
        x.to_gpu(1)
        self.check_constant_initialization(
            x, self.a, cuda.cupy, chainer.get_device((cuda.cupy, 1)))

    @attr.gpu
    def test_initialize_to_cpu(self):
        x = chainer.Parameter(initializer=initializers.Constant(self.a))
        x.to_gpu()
        x.to_cpu()
        self.check_constant_initialization(
            x, self.a, np, chainer.get_device(np))

    @attr.ideep
    def test_initialize_to_intel64(self):
        x = chainer.Parameter(initializer=initializers.Constant(self.a))
        assert x.data is None
        x.to_intel64()
        x.initialize(self.a.shape)
        assert isinstance(x.data, intel64.mdarray)
        np.testing.assert_array_equal(x.data, self.a)
        np.testing.assert_array_equal(x.grad, np.float32('nan'))

    @attr.chainerx
    def test_initialize_to_chainerx_native(self):
        x = chainer.Parameter(initializer=initializers.Constant(self.a))
        x.to_device(np)
        x.to_chainerx()
        self.check_constant_initialization(
            x, self.a, chainerx, chainer.get_device('native:0'))

    @attr.chainerx
    @attr.gpu
    def test_initialize_to_chainerx_cuda(self):
        x = chainer.Parameter(initializer=initializers.Constant(self.a))
        x.to_device((cuda.cupy, 0))
        x.to_chainerx()
        self.check_constant_initialization(
            x, self.a, chainerx, chainer.get_device('cuda:0'))

    @attr.chainerx
    @attr.multi_gpu(2)
    def test_initialize_to_chainerx_cuda_noncurrent_gpu(self):
        x = chainer.Parameter(initializer=initializers.Constant(self.a))
        x.to_device((cuda.cupy, 1))
        x.to_chainerx()
        self.check_constant_initialization(
            x, self.a, chainerx, chainer.get_device('cuda:1'))

    def test_copy_to_initialize(self):
        # This test intends the use case of link.copy() method.
        x = chainer.Parameter()
        y = copy.copy(x)
        x.initialize((3, 2))
        assert x.data is y.data

    def test_cleargrad(self):
        x = chainer.Parameter()
        x.cleargrad()
        x.initialize((3, 2))
        assert x.grad is None

    def check_zerograd(self, x, xp):
        assert isinstance(x.grad, xp.ndarray)
        assert x.grad.shape == x.data.shape
        assert x.grad.dtype == x.data.dtype
        xp.testing.assert_array_equal(x.grad, 0)

    def test_zerograd(self):
        x = chainer.Parameter()
        with testing.assert_warns(DeprecationWarning):
            x.zerograd()
        x.initialize((3, 2))
        self.check_zerograd(x, np)

    @attr.gpu
    def test_zerograd_to_gpu(self):
        x = chainer.Parameter()
        with testing.assert_warns(DeprecationWarning):
            x.zerograd()
        x.to_gpu()
        x.initialize((3, 2))
        self.check_zerograd(x, cuda.cupy)

    @attr.gpu
    def test_to_gpu_zerograd(self):
        x = chainer.Parameter()
        x.to_gpu()
        with testing.assert_warns(DeprecationWarning):
            x.zerograd()
        x.initialize((3, 2))
        self.check_zerograd(x, cuda.cupy)

    @attr.chainerx
    def test_zerograd_to_chainerx(self):
        x = chainer.Parameter()
        with testing.assert_warns(DeprecationWarning):
            x.zerograd()
        x.to_device(np)
        x.to_chainerx()
        x.initialize((3, 2))
        self.check_zerograd(x, chainerx)

    @attr.chainerx
    def test_to_chainerx_zerograd(self):
        x = chainer.Parameter()
        x.to_device(np)
        x.to_chainerx()
        with testing.assert_warns(DeprecationWarning):
            x.zerograd()
        x.initialize((3, 2))
        self.check_zerograd(x, chainerx)

    def test_zerograd_dtype(self):
        x = chainer.Parameter(initializers.Zero(dtype=np.float16))
        with testing.assert_warns(DeprecationWarning):
            x.zerograd()
        x.initialize((3, 2))
        assert x.grad.dtype == x.data.dtype

    def test_copydata_to_uninitialized_parameter(self):
        x = chainer.Parameter()
        y = chainer.Parameter(self.a)
        x.copydata(y)
        np.testing.assert_array_equal(x.data, self.a)

    @attr.gpu
    def test_copydata_to_uninitialized_parameter_gpu(self):
        x = chainer.Parameter()
        y = chainer.Parameter(self.a)
        x.to_gpu()
        x.copydata(y)
        cp = cuda.cupy
        assert isinstance(x.data, cp.ndarray)
        cp.testing.assert_array_equal(x.data, self.a)

    @attr.chainerx
    def test_copydata_to_uninitialized_parameter_chainerx(self):
        # TODO(sonots): Support copyto with ChainerX
        raise unittest.SkipTest('ChainerX does not support copyto')

    def test_copydata_from_uninitialized_parameter(self):
        initializer = initializers.Zero()
        x = chainer.Parameter(self.a)
        y = chainer.Parameter(initializer)
        x.copydata(y)
        assert isinstance(x.data, np.ndarray)
        assert isinstance(y.data, np.ndarray)
        np.testing.assert_array_equal(x.data, y.data)

    @attr.gpu
    def test_copydata_from_uninitialized_parameter_gpu(self):
        initializer = initializers.Zero()
        x = chainer.Parameter(self.a)
        y = chainer.Parameter(initializer)
        y.to_gpu()
        x.copydata(y)
        cp = cuda.cupy
        assert isinstance(x.data, np.ndarray)
        assert isinstance(y.data, cp.ndarray)
        cp.testing.assert_array_equal(x.data, y.data)

    @attr.chainerx
    def test_copydata_from_uninitialized_parameter_chainerx(self):
        # TODO(sonots): Support copydata with ChainerX
        raise unittest.SkipTest('ChainerX does not support copydata')

    def test_copydata_from_to_uninitialized_parameters(self):
        x = chainer.Parameter()
        y = chainer.Parameter()
        x.copydata(y)
        assert x.data is None
        assert y.data is None

    def test_addgrad_to_uninitialized_parameter(self):
        x = chainer.Parameter()
        y = chainer.Parameter(self.a)
        y.grad = self.b
        x.cleargrad()
        x.addgrad(y)
        assert isinstance(x.data, np.ndarray)
        assert isinstance(x.grad, np.ndarray)
        np.testing.assert_array_equal(x.grad, self.b)

    @attr.gpu
    def test_addgrad_to_uninitialized_parameter_cpu_to_gpu(self):
        x = chainer.Parameter()
        y = chainer.Parameter(self.a)
        y.grad = self.b
        x.to_gpu()
        x.cleargrad()
        x.addgrad(y)
        cp = cuda.cupy
        assert isinstance(x.data, cp.ndarray)
        assert isinstance(x.grad, cp.ndarray)
        cp.testing.assert_array_equal(x.grad, self.b)

    @attr.gpu
    def test_addgrad_to_uninitialized_parameter_gpu_to_cpu(self):
        x = chainer.Parameter()
        y = chainer.Parameter(self.a)
        y.grad = self.b
        y.to_gpu()
        x.cleargrad()
        x.addgrad(y)
        assert isinstance(x.data, np.ndarray)
        assert isinstance(x.grad, np.ndarray)
        np.testing.assert_array_equal(x.grad, self.b)

    @attr.gpu
    def test_addgrad_to_uninitialized_parameter_gpu_to_gpu(self):
        x = chainer.Parameter()
        y = chainer.Parameter(self.a)
        y.grad = self.b
        x.to_gpu()
        y.to_gpu()
        x.cleargrad()
        x.addgrad(y)
        cp = cuda.cupy
        assert isinstance(x.data, cp.ndarray)
        assert isinstance(x.grad, cp.ndarray)
        cp.testing.assert_array_equal(x.grad, self.b)

    @attr.multi_gpu(2)
    def test_addgrad_to_uninitialized_parameter_gpu_to_another_gpu(self):
        x = chainer.Parameter()
        y = chainer.Parameter(self.a)
        y.grad = self.b
        x.to_gpu(1)
        y.to_gpu(0)
        x.cleargrad()
        x.addgrad(y)
        cp = cuda.cupy
        assert isinstance(x.data, cp.ndarray)
        assert isinstance(x.grad, cp.ndarray)
        assert int(x.data.device) == 1
        assert int(x.grad.device) == 1
        cp.testing.assert_array_equal(x.grad, self.b)

    @attr.chainerx
    def test_addgrad_to_uninitialized_parameter_cpu_to_chainerx(self):
        # TODO(sonots): Support addgrad with ChainerX
        raise unittest.SkipTest('ChainerX does not support addgrad')


class TestDebugPrint(unittest.TestCase):

    def setUp(self):
        self.arr = np.random.randn(5, 3, 5, 5).astype(np.float32)

    def check_debug_print(self, v, mean, std):
        result = v.debug_print()
        assert v.summary() in result
        assert 'dtype: float32' in result
        # py2.7 on win64 returns shape as long
        assert re.match(r'- shape: \(5L?, 3L?, 5L?, 5L?\)',
                        result.splitlines()[3])

        # no grad
        msg = 'statistics: mean={mean:.8f}, std={std:.8f}'
        msg = msg.format(mean=mean, std=std)
        assert msg in result
        assert 'grad: None' in result

        # zero grad
        with testing.assert_warns(DeprecationWarning):
            v.zerograd()
        result = v.debug_print()
        assert 'grad: 0' in result

        # add grad
        v.grad = v.data
        result = v.debug_print()

        msg = 'grad: mean={mean:.8f}, std={std:.8f}'.format(mean=mean, std=std)
        assert msg in result

    def check_debug_print_empty(self, v):
        result = v.debug_print()
        assert 'device: None' in result
        assert 'backend: None' in result
        assert 'shape: None' in result
        assert 'dtype: None' in result
        assert 'statistics: None' in result
        assert 'grad: None' in result

    def test_debug_print_cpu(self):
        v = chainer.Variable(self.arr)
        result = v.debug_print()
        assert 'device: CPU' in result
        assert 'numpy.ndarray' in result

        self.check_debug_print(v, mean=float(np.mean(v.data)),
                               std=float(np.std(v.data)))

    @attr.gpu
    def test_debug_print_gpu(self):
        v = chainer.Variable(self.arr)
        v.to_gpu(0)

        result = v.debug_print()
        assert 'device: <CUDA Device 0>' in result
        assert 'cupy.core.core.ndarray' in result

        self.check_debug_print(v, mean=float(cuda.cupy.mean(v.data)),
                               std=float(cuda.cupy.std(v.data)))

    def test_debug_print_empty(self):
        v = chainer.Variable()
        self.check_debug_print_empty(v)


class TestVariableSetCreator(unittest.TestCase):

    class MockFunction(chainer.Function):
        pass

    def setUp(self):
        self.x = np.random.uniform(-1, 1, (2, 5)).astype(np.float32)
        self.f = self.MockFunction()
        self.node = self.f.node
        self.node.rank = 10

    def check_set_creator(self, x):
        x = chainer.Variable(x)
        x.set_creator(self.f)
        assert x.creator == self.f
        assert x.rank == 11

    def test_set_creator_cpu(self):
        self.check_set_creator(self.x)

    @attr.gpu
    def test_set_creator_gpu(self):
        self.check_set_creator(cuda.to_gpu(self.x))

    def check_set_creator_node(self, x):
        x = chainer.Variable(x)
        x.set_creator_node(self.node)
        assert x.creator_node == self.node
        assert x.rank == 11

    def test_set_creator_node_cpu(self):
        self.check_set_creator_node(self.x)

    @attr.gpu
    def test_set_creator_node_gpu(self):
        self.check_set_creator_node(cuda.to_gpu(self.x))


class TestVariableBackwardError(unittest.TestCase):

    def setUp(self):
        self.x = np.array([1], np.float32)

    def check_type_mismatch(self, x_data):
        xp = backend.get_array_module(x_data)

        class DummyFunction(chainer.Function):
            label = 'dummy_function'

            def forward(self, inputs):
                return xp.array(1, np.float32),

            def backward(self, inputs, grads):
                return [1]

        x = chainer.Variable(x_data)
        y = DummyFunction()(x)
        with six.assertRaisesRegex(self, TypeError, 'dummy_function'):
            y.backward()

    def test_type_mismatch_cpu(self):
        self.check_type_mismatch(self.x)

    @attr.gpu
    def test_type_mismatch_gpu(self):
        self.check_type_mismatch(cuda.to_gpu(self.x))

    def check_dtype_mismatch(self, x_data):
        xp = backend.get_array_module(x_data)

        class DummyFunction(chainer.Function):
            label = 'dummy_function'

            def forward(self, inputs):
                return xp.array(1, np.float32),

            def backward(self, inputs, grads):
                return xp.array([1], np.int32),

        x = chainer.Variable(x_data)
        y = DummyFunction()(x)
        with six.assertRaisesRegex(self, TypeError, 'dummy_function'):
            y.backward()

    def test_dtype_mismatch_cpu(self):
        self.check_dtype_mismatch(self.x)

    @attr.gpu
    def test_dtype_mismatch_gpu(self):
        self.check_dtype_mismatch(cuda.to_gpu(self.x))

    def check_shape_mismatch(self, x_data):
        xp = backend.get_array_module(x_data)

        class DummyFunction(chainer.Function):
            label = 'dummy_function'

            def forward(self, inputs):
                return xp.array(1, np.float32),

            def backward(self, inputs, grads):
                return xp.array([1, 2], np.float32),

        x = chainer.Variable(x_data)
        y = DummyFunction()(x)
        with six.assertRaisesRegex(self, ValueError, 'dummy_function'):
            y.backward()

    def test_shape_mismatch_cpu(self):
        self.check_shape_mismatch(self.x)

    @attr.gpu
    def test_shape_mismatch_gpu(self):
        self.check_shape_mismatch(cuda.to_gpu(self.x))


class TestVariableBackwardErrorTraceback(unittest.TestCase):

    def setUp(self):
        self.x = np.array([1], np.float32)
        chainer.set_debug(True)

    def tearDown(self):
        chainer.set_debug(False)

    def check_traceback(self, x_data):
        xp = backend.get_array_module(x_data)

        class DummyFunction(chainer.Function):
            label = 'dummy_function'

            def forward(self, inputs):
                return xp.array(1, np.float32),

            def backward(self, inputs, grads):
                return xp.array([1, 2], np.float32),

        x = chainer.Variable(x_data)
        line = inspect.currentframe().f_lineno + 1
        y = DummyFunction()(x)  # `line` is THIS line
        try:
            y.backward()
            self.fail()
        except ValueError as e:
            assert 'Stacktrace' in str(e)
            assert 'line %d' % line in str(e)

    def test_traceback_cpu(self):
        self.check_traceback(self.x)

    @attr.gpu
    def test_traceback_gpu(self):
        self.check_traceback(cuda.to_gpu(self.x))

    def test_raise(self):
        x = np.array([1], np.float32)
        x = chainer.Variable(x)
        y = F.identity(x)
        y.grad = np.array([np.nan], np.float32)
        with pytest.raises(RuntimeError):
            y.backward()

    def test_int(self):
        x = np.array([1], np.int)
        x = chainer.Variable(x)
        y = F.identity(x)
        y.grad = np.array([0], np.int)
        y.backward()


@testing.parameterize(*testing.product({
    'in_shape': [(4, 3, 2)],
    'out_shape': [(2, 2, 6), (2, -1, 6), 24, (-1,), [2, 12]],
    'dtype': [np.float16, np.float32, np.float64],
}))
class TestReshape(unittest.TestCase):

    def setUp(self):
        self.x = np.random.uniform(-1, 1, self.in_shape).astype(self.dtype)

    def check_forward(self, x_data):
        shape = self.out_shape
        x = chainer.Variable(x_data)
        y = x.reshape(shape)
        assert y.data.dtype == self.dtype
        assert (self.x.reshape(shape)
                == backend.CpuDevice().send(y.data)).all()

    def test_forward_cpu(self):
        self.check_forward(self.x)

    @attr.gpu
    def test_forward_gpu(self):
        self.check_forward(cuda.to_gpu(self.x))

    @attr.chainerx
    def test_forward_chainerx(self):
        # TODO(imanishi): chainerx does not support fp16 yet
        if self.dtype == np.float16:
            raise unittest.SkipTest('ChainerX does not support float16')
        self.check_forward(chainerx.array(self.x))

    def check_backward(self, x_data):
        x = chainer.Variable(x_data)
        y = x.reshape(self.out_shape)
        y.grad = y.data
        y.backward()
        testing.assert_allclose(backend.CpuDevice().send(x.data),
                                backend.CpuDevice().send(x.grad),
                                atol=0, rtol=0)

    def test_backward_cpu(self):
        self.check_backward(self.x)

    @attr.gpu
    def test_backward_gpu(self):
        self.check_backward(cuda.to_gpu(self.x))

    @attr.chainerx
    def test_backward_chainerx(self):
        # TODO(niboshi): Support it
        if self.dtype == np.float16:
            raise unittest.SkipTest('ChainerX does not support float16')
        self.check_backward(chainerx.array(self.x))


@testing.parameterize(*testing.product({
    'in_shape': [(4, 3, 2)],
    'axes': [[], [(-1, 0, 1)], [[-1, 0, 1]], [None], [-1, 0, 1]],
    'dtype': [np.float16, np.float32, np.float32],
}))
class TestTranspose(unittest.TestCase):

    def setUp(self):
        self.x = np.random.uniform(-1, 1, self.in_shape).astype(self.dtype)

    def check_forward(self, x_data):
        axes = self.axes
        x = chainer.Variable(x_data)
        y = x.transpose(*axes)
        assert y.data.dtype == self.dtype
        assert (self.x.transpose(*axes) ==
                backend.CpuDevice().send(y.data)).all()

    def test_forward_cpu(self):
        self.check_forward(self.x)

    @attr.gpu
    def test_forward_gpu(self):
        self.check_forward(cuda.to_gpu(self.x))

    @attr.chainerx
    def test_forward_chainerx(self):
        # TODO(hvy): chainerx does not support fp16 yet
        if self.dtype == np.float16:
            raise unittest.SkipTest('ChainerX does not support float16')
        self.check_forward(chainerx.array(self.x))

    def check_backward(self, x_data):
        x = chainer.Variable(x_data)
        y = x.transpose(*self.axes)
        y.grad = y.data
        y.backward()
        testing.assert_allclose(x.data, x.grad, atol=0, rtol=0)

    def test_backward_cpu(self):
        self.check_backward(self.x)

    @attr.gpu
    def test_backward_gpu(self):
        self.check_backward(cuda.to_gpu(self.x))

    @attr.chainerx
    def test_backward_chainerx(self):
        # TODO(niboshi): Support it
        if self.dtype == np.float16:
            raise unittest.SkipTest('ChainerX does not support float16')
        self.check_backward(chainerx.array(self.x))


class UnnamedVariableToStringTestBase(object):

    def setUp(self):
        if self.x_shape is None:
            self.x = chainer.Variable()
        else:
            x = np.empty(self.x_shape)
            x = np.arange(x.size).reshape(self.x_shape)
            x = x.astype(self.dtype)
            self.x = chainer.Variable(x)

    def test_repr_cpu(self):
        assert repr(self.x) == self.repr

    def test_str_cpu(self):
        assert str(self.x) == self.str

    @attr.gpu
    def test_repr_gpu(self):
        self.x.to_gpu()
        assert repr(self.x) == self.repr

    @attr.gpu
    def test_str_gpu(self):
        self.x.to_gpu()
        assert str(self.x) == self.str

    def _skip_chainerx_unsupported_dtype(self):
        supported_dtypes = chainerx.testing.dtypes.all_dtypes
        if (self.dtype is not None
                and self.dtype.__name__ not in supported_dtypes):
            raise unittest.SkipTest(
                'ChainerX does not support {} dtype'.format(
                    self.dtype.__name__))

    @attr.chainerx
    def test_repr_chainerx_cpu(self):
        self._skip_chainerx_unsupported_dtype()
        self.x.to_chainerx()
        assert repr(self.x) == self.repr

    @attr.chainerx
    def test_str_chainerx_cpu(self):
        self._skip_chainerx_unsupported_dtype()
        self.x.to_chainerx()
        assert str(self.x) == self.str

    @attr.chainerx
    @attr.gpu
    def test_repr_chainerx_gpu(self):
        self._skip_chainerx_unsupported_dtype()
        self.x.to_gpu()
        self.x.to_chainerx()
        assert repr(self.x) == self.repr

    @attr.chainerx
    @attr.gpu
    def test_str_chainerx_gpu(self):
        self._skip_chainerx_unsupported_dtype()
        self.x.to_gpu()
        self.x.to_chainerx()
        assert str(self.x) == self.str


@testing.parameterize(
    {'x_shape': None, 'dtype': None, 'repr': 'variable(None)',
     'str': 'variable(None)'},
    {'x_shape': (2, 2,), 'dtype': np.float16,
     'repr': 'variable([[ 0.,  1.],\n          [ 2.,  3.]])',
     'str': 'variable([[ 0.  1.]\n          [ 2.  3.]])'},
    {'x_shape': (2, 2,), 'dtype': np.float32,
     'repr': 'variable([[ 0.,  1.],\n          [ 2.,  3.]])',
     'str': 'variable([[ 0.  1.]\n          [ 2.  3.]])'},
    {'x_shape': (2, 2,), 'dtype': np.float64,
     'repr': 'variable([[ 0.,  1.],\n          [ 2.,  3.]])',
     'str': 'variable([[ 0.  1.]\n          [ 2.  3.]])'},
    {'x_shape': (3,), 'dtype': np.float32,
     'repr': 'variable([ 0.,  1.,  2.])', 'str': 'variable([ 0.  1.  2.])'},
)
@testing.with_requires('numpy<1.14')
class TestUnnamedVariableToStringLegacy(
        UnnamedVariableToStringTestBase, unittest.TestCase):
    # Textual representation of arrays in NumPy 1.13 or earlier.
    pass


@testing.parameterize(
    {'x_shape': None, 'dtype': None, 'repr': 'variable(None)',
     'str': 'variable(None)'},
    {'x_shape': (2, 2,), 'dtype': np.float16,
     'repr': 'variable([[0., 1.],\n          [2., 3.]])',
     'str': 'variable([[0. 1.]\n          [2. 3.]])'},
    {'x_shape': (2, 2,), 'dtype': np.float32,
     'repr': 'variable([[0., 1.],\n          [2., 3.]])',
     'str': 'variable([[0. 1.]\n          [2. 3.]])'},
    {'x_shape': (2, 2,), 'dtype': np.float64,
     'repr': 'variable([[0., 1.],\n          [2., 3.]])',
     'str': 'variable([[0. 1.]\n          [2. 3.]])'},
    {'x_shape': (3,), 'dtype': np.float32,
     'repr': 'variable([0., 1., 2.])', 'str': 'variable([0. 1. 2.])'},
)
@testing.with_requires('numpy>=1.14')
class TestUnnamedVariableToStringModern(
        UnnamedVariableToStringTestBase, unittest.TestCase):
    # Textual representation of arrays in NumPy 1.14 or later.
    pass


class TestUnnamedVariableDim2Size0ToString(unittest.TestCase):

    def setUp(self):
        x = np.empty((0, 0))
        x = x.astype(np.float32)
        self.x = chainer.Variable(x)
        if (sys.version_info < (3,) and sys.maxsize > 2**32 and
                platform.system() == 'Windows'):
            self.repr = 'variable([], shape=(0L, 0L))'
        else:
            self.repr = 'variable([], shape=(0, 0))'
        self.str = 'variable([])'

    def test_repr_cpu(self):
        assert repr(self.x) == self.repr

    def test_str_cpu(self):
        assert str(self.x) == self.str

    @attr.gpu
    def test_repr_gpu(self):
        self.x.to_gpu()
        assert repr(self.x) == self.repr

    @attr.gpu
    def test_str_gpu(self):
        self.x.to_gpu()
        assert str(self.x) == self.str


class NamedVariableToStringTestBase(object):

    def setUp(self):
        if self.x_shape is None:
            self.x = chainer.Variable(name='x')
        else:
            x = np.empty(self.x_shape)
            x = np.arange(x.size).reshape(self.x_shape)
            x = x.astype(self.dtype)
            self.x = chainer.Variable(x, name='x')

    def test_named_repr(self):
        assert repr(self.x) == self.repr

    def test_named_str(self):
        assert str(self.x) == self.str

    @attr.gpu
    def test_repr_gpu(self):
        self.x.to_gpu()
        assert repr(self.x) == self.repr

    @attr.gpu
    def test_str_gpu(self):
        self.x.to_gpu()
        assert str(self.x) == self.str


@testing.parameterize(
    {'x_shape': None, 'dtype': None, 'repr': 'variable x(None)',
     'str': 'variable x(None)'},
    {'x_shape': (2, 2,), 'dtype': np.float32,
     'repr': 'variable x([[ 0.,  1.],\n            [ 2.,  3.]])',
     'str': 'variable x([[ 0.  1.]\n            [ 2.  3.]])'},
    {'x_shape': (), 'dtype': np.float32,
     'repr': 'variable x(0.0)', 'str': 'variable x(0.0)'},
)
@testing.with_requires('numpy<1.14')
class TestNamedVariableToStringLegacy(
        NamedVariableToStringTestBase, unittest.TestCase):
    # Textual representation of arrays in NumPy 1.13 or earlier.
    pass


@testing.parameterize(
    {'x_shape': None, 'dtype': None, 'repr': 'variable x(None)',
     'str': 'variable x(None)'},
    {'x_shape': (2, 2,), 'dtype': np.float32,
     'repr': 'variable x([[0., 1.],\n            [2., 3.]])',
     'str': 'variable x([[0. 1.]\n            [2. 3.]])'},
    {'x_shape': (), 'dtype': np.float32,
     'repr': 'variable x(0.)', 'str': 'variable x(0.)'},
)
@testing.with_requires('numpy>=1.14')
class TestNamedVariableToStringModern(
        NamedVariableToStringTestBase, unittest.TestCase):
    # Textual representation of arrays in NumPy 1.14 or later.
    pass


class TestNamedVariableDim2Size0ToString(unittest.TestCase):

    def setUp(self):
        x = np.empty((0, 0))
        x = x.astype(np.float32)
        self.x = chainer.Variable(x, name='x')
        if (sys.version_info < (3,) and sys.maxsize > 2**32 and
                platform.system() == 'Windows'):
            self.repr = 'variable x([], shape=(0L, 0L))'
        else:
            self.repr = 'variable x([], shape=(0, 0))'
        self.str = 'variable x([])'

    def test_named_repr(self):
        assert repr(self.x) == self.repr

    def test_named_str(self):
        assert str(self.x) == self.str

    @attr.gpu
    def test_repr_gpu(self):
        self.x.to_gpu()
        assert repr(self.x) == self.repr

    @attr.gpu
    def test_str_gpu(self):
        self.x.to_gpu()
        assert str(self.x) == self.str


class IdentityFunction(chainer.Function):

    def forward(self, inputs):
        return inputs

    def backward(self, inputs, grad_outputs):
        return grad_outputs


class TestVariableDoubleBackward(unittest.TestCase):

    def test_default_backward(self):
        x = chainer.Variable(np.empty((), np.float32))
        y = x * 2
        y.backward()
        assert x.grad_var is not y.grad_var
        assert x.grad_var.creator is None
        x.grad_var.backward()
        assert y.grad_var.grad_var is None

    def test_raise_double_backprop(self):
        x = chainer.Variable(np.empty((), np.float32))
        y = IdentityFunction()(x)
        y.backward(enable_double_backprop=True)
        with pytest.raises(RuntimeError):
            x.grad_var.backward()

    def test_raise_double_backprop_2(self):
        x = chainer.Variable(np.empty((), np.float32))
        z = F.identity(x)  # new style
        y = IdentityFunction()(z)  # old style
        y.backward(enable_double_backprop=True)
        with pytest.raises(RuntimeError):
            x.grad_var.backward()

    def test_grad_raise_double_backprop(self):
        x = chainer.Variable(np.empty((), np.float32))
        y = IdentityFunction()(x)
        y.backward(enable_double_backprop=True)
        with pytest.raises(RuntimeError):
            chainer.grad([x.grad_var], [y.grad_var])

    def test_grad_raise_double_backprop_2(self):
        x = chainer.Variable(np.empty((), np.float32))
        z = F.identity(x)  # new style
        y = IdentityFunction()(z)  # old style
        y.backward(enable_double_backprop=True)
        with pytest.raises(RuntimeError):
            chainer.grad([x.grad_var], [y.grad_var])


class TestVariableDoubleBackwardOneElementScalar(unittest.TestCase):
    # Tests for old-styled (1-element array) scalar.
    # See: https://github.com/chainer/chainer/pull/4199

    def test_default_backward(self):
        x = chainer.Variable(np.empty(1, np.float32))
        y = F.identity(x)
        with testing.assert_warns(DeprecationWarning):
            y.backward()
        assert x.grad_var.creator is None
        x.grad_var.backward()
        assert y.grad_var.grad_var is None

    def test_raise_double_backprop(self):
        x = chainer.Variable(np.empty(1, np.float32))
        y = IdentityFunction()(x)
        with testing.assert_warns(DeprecationWarning):
            y.backward(enable_double_backprop=True)
        with pytest.raises(RuntimeError):
            x.grad_var.backward()

    def test_raise_double_backprop_2(self):
        x = chainer.Variable(np.empty(1, np.float32))
        z = F.identity(x)  # new style
        y = IdentityFunction()(z)  # old style
        with testing.assert_warns(DeprecationWarning):
            y.backward(enable_double_backprop=True)
        with pytest.raises(RuntimeError):
            x.grad_var.backward()

    def test_grad_raise_double_backprop(self):
        x = chainer.Variable(np.empty(1, np.float32))
        y = IdentityFunction()(x)
        with testing.assert_warns(DeprecationWarning):
            y.backward(enable_double_backprop=True)
        with pytest.raises(RuntimeError):
            chainer.grad([x.grad_var], [y.grad_var])

    def test_grad_raise_double_backprop_2(self):
        x = chainer.Variable(np.empty(1, np.float32))
        z = F.identity(x)  # new style
        y = IdentityFunction()(z)  # old style
        with testing.assert_warns(DeprecationWarning):
            y.backward(enable_double_backprop=True)
        with pytest.raises(RuntimeError):
            chainer.grad([x.grad_var], [y.grad_var])


class TestAsVariable(unittest.TestCase):

    def check_to_variable_from_array(self, x):
        y = chainer.as_variable(x)
        assert isinstance(y, chainer.Variable)
        assert y.data is x
        assert not y.requires_grad

    def test_to_variable_from_numpy(self):
        self.check_to_variable_from_array(np.empty(1, np.float32))

    @attr.gpu
    def test_to_variable_from_cupy(self):
        self.check_to_variable_from_array(cuda.cupy.empty(1, np.float32))

    def test_to_variable_from_variable(self):
        x = chainer.Variable(np.array(1, np.float32))
        y = chainer.as_variable(x)
        assert x is y
        assert y.requires_grad


@testing.parameterize(*testing.product({
    'in_shape': [(4, 3, 2)],
    'dtype': [np.float16, np.float32, np.float64],
    'loss_scale': [None, 1, 10],
}))
class TestLossScale(unittest.TestCase):

    def setUp(self):
        self.x = np.random.uniform(-1, 1, self.in_shape).astype(self.dtype)
        self.y = np.random.uniform(-1, 1, self.in_shape).astype(self.dtype)

    def check_loss_scale(self, x_data, y_data):
        x = chainer.Variable(x_data)
        y = chainer.Variable(y_data)
        z = x * y
        loss = F.sum(z)
        loss.backward(loss_scale=self.loss_scale)
        if self.loss_scale is not None:
            x.grad /= self.loss_scale
            y.grad /= self.loss_scale
        rtol, atol = 1e-4, 1e-5
        if self.dtype is np.float16:
            rtol, atol = 1e-1, 1e-2
        testing.assert_allclose(x.data, y.grad, rtol=rtol, atol=atol)
        testing.assert_allclose(y.data, x.grad, rtol=rtol, atol=atol)

    def test_loss_scale_cpu(self):
        self.check_loss_scale(self.x, self.y)

    @attr.gpu
    def test_loss_scale_gpu(self):
        self.check_loss_scale(cuda.to_gpu(self.x), cuda.to_gpu(self.y))


@testing.parameterize(*testing.product({
    # ideep2.0.0 not support shape 0
    'shape': [(1,), (3, 2), (2, 3, 4, 3)],
    'dtype': [
        np.int8, np.int16, np.int32, np.int64, np.uint8, np.uint16, np.uint32,
        np.uint64, np.float16, np.float32, np.float64],
}))
@attr.ideep
class TestIntel64(unittest.TestCase):
    def setUp(self):
        self.x_data = np.random.uniform(-1, 1, self.shape).astype(self.dtype)

    def _check_variable_shape_and_dtype(self, var):
        assert var.data.shape == self.shape
        assert var.data.dtype == self.dtype
        assert var.shape == self.shape
        assert var.dtype == self.dtype

    def test_cpu_to_intel64(self):
        x = chainer.Variable(self.x_data)
        assert x.xp is np
        prev_x_data = x.data
        x.to_intel64()
        assert x.xp is np

        # Converted to mdarray only if dtype == float32.
        # Otherwise, data should be left untouched.
        if self.dtype == np.float32:
            assert isinstance(x.data, intel64.ideep.mdarray)
        else:
            assert x.data is prev_x_data

        self._check_variable_shape_and_dtype(x)

    def test_intel64_to_intel64(self):
        x = chainer.Variable(self.x_data)
        x.to_intel64()
        prev_x_data = x.data
        x.to_intel64()

        # Data should be left untouched
        assert x.data is prev_x_data

    @attr.gpu
    def test_gpu_to_intel64(self):
        x = chainer.Variable(self.x_data)
        x.to_gpu()
        x.to_intel64()

        # Converted to mdarray only if dtype == float32.
        # Otherwise, data should be converted to numpy.ndarray.
        if self.dtype == np.float32:
            assert isinstance(x.data, intel64.ideep.mdarray)
        else:
            assert isinstance(x.data, np.ndarray)

        self._check_variable_shape_and_dtype(x)

    @attr.gpu
    def test_intel64_to_gpu(self):
        x = chainer.Variable(self.x_data)
        x.to_intel64()
        x.to_gpu()

        # Data should be converted to cuda.ndarray
        assert isinstance(x.data, cuda.cupy.ndarray)
        self._check_variable_shape_and_dtype(x)

    def test_intel64_to_cpu(self):
        x = chainer.Variable(self.x_data)
        x.to_intel64()
        x.to_cpu()

        # Data should be converted to numpy.ndarray
        assert isinstance(x.data, np.ndarray)
        self._check_variable_shape_and_dtype(x)


@testing.parameterize(*testing.product({
    'shape': [(), (3, 2, 3), (4, 4, 3, 2, 3)],
    'dtype': [
        np.int8, np.int16, np.int32, np.int64,
        np.uint8, np.uint16, np.uint32, np.uint64,
        np.float16, np.float32, np.float64,
    ],
}))
@attr.ideep
class TestIntel64Unsupported(unittest.TestCase):

    """Tests for arrays that should not be converted to iDeep array."""

    def setUp(self):
        self.x_data = np.random.uniform(-1, 1, self.shape).astype(self.dtype)

    def test_cpu_to_intel64(self):
        x = chainer.Variable(self.x_data)
        x.to_intel64()
        assert isinstance(x.data, np.ndarray)

    @attr.gpu
    def test_gpu_to_intel64(self):
        x = chainer.Variable(self.x_data)
        x.to_gpu()
        x.to_intel64()
        assert isinstance(x.data, np.ndarray)


@testing.parameterize(*testing.product({
    'shape': [(3,), (3, 2), (3, 2, 2), (3, 2, 2, 3)],
    'dtype': [
        np.int8, np.int16, np.int32, np.int64, np.uint8, np.uint16, np.uint32,
        np.uint64, np.float16, np.float32, np.float64],
}))
class TestLazyGradSum(unittest.TestCase):

    def setUp(self):
        self.x = np.random.uniform(-1, 1, self.shape).astype(self.dtype)

        y10 = np.random.uniform(-1, 1, self.shape).astype(self.dtype)
        gy00 = chainer.Variable(
            np.random.uniform(-1, 1, self.shape).astype(self.dtype))
        f10 = chainer.FunctionNode()
        f10.check_type_forward = mock.MagicMock()
        f10.forward_cpu = mock.MagicMock(return_value=(y10,))
        f10.retain_outputs((0,))
        f10.backward = mock.MagicMock(return_value=(gy00,))
        self.y10 = y10
        self.f10 = f10
        self.gy00 = gy00

        y11 = np.random.uniform(-1, 1, self.shape).astype(self.dtype)
        gy01 = chainer.Variable(
            np.random.uniform(-1, 1, self.shape).astype(self.dtype))
        f11 = chainer.FunctionNode()
        f11.check_type_forward = mock.MagicMock()
        f11.forward_cpu = mock.MagicMock(return_value=(y11,))
        f11.retain_outputs((0,))
        f11.backward = mock.MagicMock(return_value=(gy01,))
        self.y11 = y11
        self.f11 = f11
        self.gy01 = gy01

        y12 = np.random.uniform(-1, 1, self.shape).astype(self.dtype)
        gy02 = chainer.Variable(
            np.random.uniform(-1, 1, self.shape).astype(self.dtype))
        f12 = chainer.FunctionNode()
        f12.check_type_forward = mock.MagicMock()
        f12.forward_cpu = mock.MagicMock(return_value=(y12,))
        f12.retain_outputs((0,))
        f12.backward = mock.MagicMock(return_value=(gy02,))
        self.y12 = y12
        self.f12 = f12
        self.gy02 = gy02

        y = np.random.uniform(-1, 1, self.shape).astype(self.dtype)
        gy10 = chainer.Variable(
            np.random.uniform(-1, 1, self.shape).astype(self.dtype))
        gy11 = chainer.Variable(
            np.random.uniform(-1, 1, self.shape).astype(self.dtype))
        gy12 = chainer.Variable(
            np.random.uniform(-1, 1, self.shape).astype(self.dtype))
        f2 = chainer.FunctionNode()
        f2.check_type_forward = mock.MagicMock()
        f2.forward_cpu = mock.MagicMock(return_value=(y,))
        f12.retain_outputs((0,))
        f2.backward = mock.MagicMock(return_value=(gy10, gy11, gy12))
        self.y = y
        self.f2 = f2
        self.gy10 = gy10
        self.gy11 = gy11
        self.gy12 = gy12
        self.gx = gy00 + gy01 + gy02

    def forward(self, x):
        y0 = F.identity(x)
        y10 = self.f10.apply((y0,))
        y11 = self.f11.apply((y0,))
        y12 = self.f12.apply((y0,))
        y = self.f2.apply((y10[0], y11[0], y12[0]))
        return y

    def check_backward(self):
        x = chainer.Variable(self.x)
        y = self.forward(x)
        y[0].grad = np.ones(y[0].shape, y[0].dtype)
        y[0].backward()
        testing.assert_allclose(self.gx.data, x.grad, atol=1e-3, rtol=1e-2)

    def test_backward_cpu(self):
        with chainer.using_config('lazy_grad_sum', False):
            self.check_backward()

    def test_backward_cpu_lazy_grad_sum(self):
        with chainer.using_config('lazy_grad_sum', True):
            self.check_backward()


testing.run_module(__name__, __file__)
