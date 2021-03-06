# inspired by https://github.com/karpathy/micrograd/blob/master/micrograd/engine.py
from inspect import signature
import numpy as np
import os
try:
  import pyopencl as cl # pyopencl 科学计算库，gpu加速
  GPU = True
except ImportError:
  # no GPU support
  GPU = False
  
# **** Python中带下划线的变量和方法总结 —— https://blog.csdn.net/tcx1992/article/details/80105645 ****
# **** 1、单前导下划线 _var ：非强制约定该变量或方法仅内部使用（私有）。****
# **** 2、单末尾下划线 var_ ：避免与关键字的命名冲突。****
# **** 3、双前导下划线 __var ：导致Python解释器重写属性名称，以避免子类中的命名冲突， 防止变量（方法）在子类中被重写。****
# **** 4、双前导和双末尾下划线 _var_ ：不会采用名称修饰，用于特殊用途，不建议使用。例子有：，__init__对象构造函数，或__call__，它使得一个对象可以被调用。 ****
# **** 5、 单下划线 _ ： 表示无关紧要的、临时的变量。 ****

# **** profiler, 10 lines too long ****
DEBUG = os.getenv("DEBUG", None) is not None
if DEBUG:
  import collections, atexit, time
  # defaultdict()中参数为字典value的类型
  debug_counts = collections.defaultdict(int)
  debug_times = collections.defaultdict(float)
  def print_debug_exit():
    # sorted方法 第一个参数：可迭代对象 第二个参数：排序的属性 第三个参数：排序规则，reverse是否降序
    # 这里取倒序
    for name, _ in sorted(debug_times.items(), key=lambda x: -x[1]):
      print("%20s : %3d  %10.2f ms" % (name, debug_counts[name], debug_times[name]))
  # python atexit 模块定义了一个 register 函数，用于在 python 解释器中注册一个退出函数，这个函数在解释器正常终止时自动执行,一般用来做一些资源清理的操作。 
  # atexit 按注册的相反顺序执行这些函数; 例如注册A、B、C，在解释器终止时按顺序C，B，A运行。
  # Note：如果程序是非正常crash，或者通过os._exit()退出，注册的退出函数将不会被调用。
  # print_debug_exit 即为注册的退出函数
  atexit.register(print_debug_exit)

class ProfileOp:
  def __init__(self, name, x, backward=False):
    self.name = ("back_" if backward else "")+name
    self.x = x
  def __enter__(self):
    if DEBUG: self.st = time.time()
  def __exit__(self, *junk):
    if DEBUG:
      et = (time.time()-self.st)*1000.
      debug_counts[self.name] += 1
      debug_times[self.name] += et
      print("%20s : %7.2f ms  %s" % (self.name, et, [y.shape for y in self.x]))

cl_ctx, cl_queue = None, None
# gpu初始化
def require_init_gpu():
  global cl_ctx, cl_queue
  if cl_queue is None:
    try:
      # for Macbook 16 inch
      cl_ctx = cl.create_some_context(answers=[0,2])
    except (cl._cl.RuntimeError, cl._cl.LogicError, TypeError):
      cl_ctx = cl.create_some_context(interactive=False)
    cl_queue = cl.CommandQueue(cl_ctx)

# **** start with two base classes ****

class Tensor:
  did_float_warning = False
  default_gpu = False

  # 属性（成员变量）：gpu、data、grad、_ctx
  def __init__(self, data, gpu=None):
    if gpu is None:
      gpu = Tensor.default_gpu
    if isinstance(data, list):
      data = np.array(data, dtype=np.float32)
    elif GPU and isinstance(data, cl._cl.Buffer):
      self.gpu = True
    elif not isinstance(data, np.ndarray):
      raise TypeError("Error constructing tensor with %r" % data)

    if isinstance(data, np.ndarray):
      if data.dtype != np.float32 and not Tensor.did_float_warning:
        # warning? float64 is actually needed for numerical jacobian
        print("warning, %r isn't float32" % (data.shape,))
        Tensor.did_float_warning = True
      self.gpu = False

    self.data = data
    # grad是Tensor
    self.grad = None

    if gpu:
      self.cuda_()

    # internal variables used for autograd graph construction
    # _ctx是一个Function，当前环境上下文是在哪个函数内部
    self._ctx = None

  def __repr__(self):
    return "Tensor %r with grad %r" % (self.data, self.grad.data if self.grad else None)

  def assign(self, x):
    self.data = x.data

  #将类方法转为类属性
  @property
  def shape(self):
    return self.data.shape

  # 参数前加*号，其类型为元组
  @staticmethod
  def zeros(*shape):
    return Tensor(np.zeros(shape, dtype=np.float32))

  @staticmethod
  def ones(*shape):
    return Tensor(np.ones(shape, dtype=np.float32))

  @staticmethod
  def randn(*shape):
    return Tensor(np.random.randn(*shape).astype(np.float32))

  @staticmethod
  def eye(dim):
    return Tensor(np.eye(dim).astype(np.float32))

  # 反向传播
  def backward(self, allow_fill=True):
    if self._ctx is None:
      return

    if self.grad is None and allow_fill:
      # fill in the first grad with one
      # this is "implicit gradient creation" 隐式梯度构造
      assert self.data.shape == (1,)
      self.grad = Tensor(np.ones(self.data.shape, dtype=self.data.dtype), gpu=self.gpu)
    
    # nodes是一个Tensor列表，node为Tensor
    visited, nodes = set(), []
    # 递归访问
    def deepwalk(node):
      visited.add(node)
      if node._ctx:
        for i in node._ctx.parents:
          if i not in visited:
            deepwalk(i)
        nodes.append(node)
    deepwalk(self)

    for t0 in reversed(nodes):
      assert (t0.grad is not None)
      # __class__.__name__ 类名
      # 在调试环境下进行反向传播（梯度计算）
      with ProfileOp(t0._ctx.__class__.__name__, [t0.grad], backward=True):
        grads = t0._ctx.backward(t0._ctx, t0.grad.data)
      # grads放到列表中，以便下面应用zip方法
      if len(t0._ctx.parents) == 1:
        grads = [grads]
      for t,g in zip(t0._ctx.parents, grads):
        if g is None:
          continue
        assert g.shape == t.data.shape, \
          "grad shape must match tensor shape in %r, %r != %r" % (self._ctx, g.shape, t.data.shape)
        # 在backward中作了链式的乘法运算，这里将不同parents对该变量的梯度（偏导数）再求和
        t.grad = Tensor(g) if t.grad is None else (t.grad + Tensor(g))

  # ***** tinygrad supports CPU and GPU *****

  def cpu(self):
    # 若原始设置在gpu上则需要迁移修改至cpu,否则不变
    if self.gpu:
      ret = Tensor(np.empty(self.shape, dtype=np.float32), gpu=False)
      cl.enqueue_copy(cl_queue, ret.data, self.data)
      if self.grad:
        ret.grad = self.grad.cpu()
      return ret
    else:
      return self

  def cuda_(self):
    # 将数据迁移到gpu上
    self.data = self.cuda().data
    self.gpu = True

  def cuda(self):
    # 若原始设置在cpu上，则需要迁移修改，否则不变
    if not GPU:
      raise Exception("No GPU Support, install pyopencl")
    if not self.gpu:
      require_init_gpu()
      assert self.data.dtype == np.float32   # only float32 on GPU
      data = cl.Buffer(cl_ctx, cl.mem_flags.READ_ONLY | cl.mem_flags.COPY_HOST_PTR, hostbuf=self.data.ravel())
      data.shape = self.shape
      data.dtype = self.data.dtype
      ret = Tensor(data)
      if self.grad:
        ret.grad = self.grad.cuda()
      return ret
    else:
      return self

  def detach(self):
    return Tensor(self.data, self.gpu)

  # ***** put ops in these dicts *****

  ops = {}
  opsgpu = {}

  # ***** non first class ops *****
  # ***** 一些稍复杂的算子 ****

  def mean(self):
    div = Tensor(np.array([1/np.prod(self.shape)], dtype=self.data.dtype), gpu=self.gpu)
    return self.sum().mul(div)

  def sqrt(self):
    root = Tensor(np.zeros(self.shape, dtype=self.data.dtype)+0.5, gpu=self.gpu)
    return self.pow(root)

  def div(self, y):
    root = Tensor(np.zeros(self.shape, dtype=self.data.dtype)-1, gpu=self.gpu)
    return self.mul(y.pow(root))

  def swish(self):
    return self.mul(self.sigmoid())

  def tanh(self):
    t2 = Tensor(np.zeros(self.shape, dtype=self.data.dtype)+2, gpu=self.gpu)
    t1 = Tensor(np.zeros(self.shape, dtype=self.data.dtype)+1, gpu=self.gpu)
    return self.mul(t2).sigmoid().mul(t2) - t1 # 2*sigmoid(2*x)-1

# An instantiation of the Function is the Context
class Function:
  def __init__(self, *tensors):
    self.parents = tensors
    self.saved_tensors = []
    
  # 保存变量x（参数）到saved_tensors列表中
  def save_for_backward(self, *x):
    self.saved_tensors.extend(x)

  def apply(self, *x, **kwargs):
    op = self
    # x——tensors,tensor列表
    ctx = op(*x)
    # use default params
    params = signature(op.forward).parameters
    for p in params.values():
      if p.default is not p.empty:
        setattr(ctx, p.name, p.default)
    # overwrite with passed params
    for k, v in kwargs.items():
      setattr(ctx, k, v)
    # 前向运算
    with ProfileOp(ctx.__class__.__name__, x):
      ret = Tensor(op.forward(ctx, *[t.data for t in x], **kwargs))
    ret._ctx = ctx
    return ret

# 注册函数,fxn函数
def register(name, fxn, gpu=False):
  if gpu:
    # opsgpu和ops字典
    Tensor.opsgpu[name] = fxn
  else:
    Tensor.ops[name] = fxn
  def dispatch(*x, **kwargs):
    f = (Tensor.opsgpu if x[0].gpu else Tensor.ops)[name]
    f.cl_ctx, f.cl_queue = cl_ctx, cl_queue
    return f.apply(f, *x, **kwargs)
  setattr(Tensor, name, dispatch)
  if name in ['add', 'sub', 'mul', 'div']:
    setattr(Tensor, "__%s__" % name, dispatch)
    setattr(Tensor, "__i%s__" % name, lambda self,x: self.assign(dispatch(self,x)))


# this registers all the operations
import tinygrad.ops
if GPU:
  import tinygrad.opsgpu

