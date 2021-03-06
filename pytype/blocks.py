"""Functions for computing the execution order of bytecode."""

import collections
import itertools

from pytype.pyc import loadmarshal
from pytype.pyc import opcodes
from pytype.pyc import pyc
from pytype.typegraph import cfg_utils

STORE_OPCODES = (
    opcodes.STORE_NAME,
    opcodes.STORE_FAST,
    opcodes.STORE_ATTR,
    opcodes.STORE_DEREF,
    opcodes.STORE_GLOBAL)

# Opcodes whose argument can be a block of code.
CODE_LOADING_OPCODES = (opcodes.LOAD_CONST,)


class OrderedCode(object):
  """Code object which knows about instruction ordering.

  Attributes:
    co_argcount: Same as loadmarshal.CodeType.
    co_kwonlyargcount: Same as loadmarshal.CodeType.
    co_nlocals: Same as loadmarshal.CodeType.
    co_stacksize: Same as loadmarshal.CodeType.
    co_flags: Same as loadmarshal.CodeType.
    co_code: Same as loadmarshal.CodeType.
    co_consts: Same as loadmarshal.CodeType.
    co_names: Same as loadmarshal.CodeType.
    co_varnames: Same as loadmarshal.CodeType.
    co_filename: Same as loadmarshal.CodeType.
    co_name: Same as loadmarshal.CodeType.
    co_firstlineno: Same as loadmarshal.CodeType.
    co_lnotab: Same as loadmarshal.CodeType.
    co_freevars: Same as loadmarshal.CodeType.
    co_cellvars: Same as loadmarshal.CodeType.
    order: A list of bytecode blocks. They're ordered ancestors-first, see
      cfg_utils.py:order_nodes.
    python_version: The Python version this bytecode is from.
  """

  def __init__(self, code, bytecode, order, python_version):
    # Copy all "co_*" attributes from code.
    # This is preferable to both inheritance (because we don't want to be
    # compatible with the base class, which is too low level) as well as
    # object composition (because that would make the API too clunky for
    # callers).
    assert hasattr(code, "co_code")
    self.__dict__.update({name: value for name, value in code.__dict__.items()
                          if name.startswith("co_")})
    self.order = order
    self.python_version = python_version
    # Store the "nice" version of the bytecode under co_code. We never claimed
    # to be compatible with CodeType.
    self.co_code = bytecode
    for insn in bytecode:
      insn.code = self

  def has_opcode(self, op_type):
    return any(isinstance(op, op_type)
               for op in itertools.chain(*(block.code for block in self.order)))

  def has_iterable_coroutine(self):
    return bool(self.co_flags & loadmarshal.CodeType.CO_ITERABLE_COROUTINE)

  def set_iterable_coroutine(self):
    self.co_flags |= loadmarshal.CodeType.CO_ITERABLE_COROUTINE

  def has_coroutine(self):
    return bool(self.co_flags & loadmarshal.CodeType.CO_COROUTINE)

  def has_generator(self):
    return bool(self.co_flags & loadmarshal.CodeType.CO_GENERATOR)

  def has_async_generator(self):
    return bool(self.co_flags & loadmarshal.CodeType.CO_ASYNC_GENERATOR)

  def has_varargs(self):
    return bool(self.co_flags & loadmarshal.CodeType.CO_VARARGS)

  def has_varkeywords(self):
    return bool(self.co_flags & loadmarshal.CodeType.CO_VARKEYWORDS)

  def has_newlocals(self):
    return bool(self.co_flags & loadmarshal.CodeType.CO_NEWLOCALS)

  def get_arg_count(self):
    count = self.co_argcount + max(self.co_kwonlyargcount, 0)
    if self.has_varargs():
      count += 1
    if self.has_varkeywords():
      count += 1
    return count


class Block(object):
  """A block is a node in a directed graph.

  It has incoming and outgoing edges (jumps). Incoming jumps always jump
  to the first instruction of our bytecode, and outgoing jumps always jump
  from the last instruction. There are no jump instructions in the middle of
  a byte code block.
  A block implements most of the "sequence" interface, i.e., it can be used as
  if it was a Python list of bytecode instructions.

  Attributes:
    id: Block id
    code: A bytecode object (a list of instances of opcodes.Opcode).
    incoming: Incoming edges. These are blocks that jump to the first
      instruction in our code object.
    outgoing: Outgoing edges. These are the targets jumped to by the last
      instruction in our code object.
  """

  def __init__(self, code):
    self.id = code[0].index
    self.code = code
    self.incoming = set()
    self.outgoing = set()

  def connect_outgoing(self, target):
    """Add an outgoing edge."""
    self.outgoing.add(target)
    target.incoming.add(self)

  def __str__(self):
    return "<Block %d>" % self.id

  def __repr__(self):
    return "<Block %d: %r>" % (self.id, self.code)

  def __getitem__(self, index_or_slice):
    return self.code.__getitem__(index_or_slice)

  def __iter__(self):
    return self.code.__iter__()


def add_pop_block_targets(bytecode):
  """Modifies bytecode so that each POP_BLOCK has a block_target.

  This is to achieve better initial ordering of try/except and try/finally code.
  try:
    i = 1
    a[i]
  except IndexError:
    return i
  By connecting a CFG edge from the end of the block (after the "a[i]") to the
  except handler, our basic block ordering algorithm knows that the except block
  needs to be scheduled last, whereas if there only was an edge before the
  "i = 1", it would be able to schedule it too early and thus encounter an
  undefined variable. This is only for ordering. The actual analysis of the
  code happens later, in vm.py.

  Args:
    bytecode: An array of bytecodes.
  """
  if not bytecode:
    return

  for op in bytecode:
    op.block_target = None

  todo = [(bytecode[0], ())]  # unordered queue of (position, block_stack)
  seen = set()
  while todo:
    op, block_stack = todo.pop()
    if op in seen:
      continue
    else:
      seen.add(op)

    # Compute the block stack
    if isinstance(op, opcodes.POP_BLOCK):
      assert block_stack, "POP_BLOCK without block."
      op.block_target = block_stack[-1].target
      block_stack = block_stack[0:-1]
    elif isinstance(op, opcodes.RAISE_VARARGS):
      # Make "raise" statements jump to the innermost exception handler.
      # (If there's no exception handler, do nothing.)
      for b in reversed(block_stack):
        if isinstance(b, opcodes.SETUP_EXCEPT):
          op.block_target = b.target
          break
    elif isinstance(op, opcodes.BREAK_LOOP):
      # Breaks jump to after the loop
      for i in reversed(range(len(block_stack))):
        b = block_stack[i]
        if isinstance(b, opcodes.SETUP_LOOP):
          op.block_target = b.target
          assert b.target != op
          todo.append((op.block_target, block_stack[0:i]))
          break
    elif isinstance(op, opcodes.SETUP_EXCEPT):
      # Exceptions pop the block, so store the previous block stack.
      todo.append((op.target, block_stack))
      block_stack += (op,)
    elif op.pushes_block():
      assert op.target, "%s without target" % op.name
      # We push the entire opcode onto the block stack, for better debugging.
      block_stack += (op,)
    elif op.does_jump() and op.target:
      todo.append((op.target, block_stack))

    if not op.no_next():
      assert op.next, "Bad instruction at end of bytecode."
      todo.append((op.next, block_stack))


def _split_bytecode(bytecode):
  """Given a sequence of bytecodes, return basic blocks.

  This will split the code at "basic block boundaries". These occur at
  every instruction that is jumped to, and after every instruction that jumps
  somewhere else (or returns / aborts).

  Args:
    bytecode: A list of instances of opcodes.Opcode. (E.g. returned from
      opcodes.dis())

  Returns:
    A list of _Block instances.
  """
  targets = {op.target for op in bytecode if op.target}
  blocks = []
  code = []
  for op in bytecode:
    code.append(op)
    if (op.no_next() or op.does_jump() or op.pops_block() or
        op.next is None or op.next in targets):
      blocks.append(Block(code))
      code = []
  return blocks


def compute_order(bytecode):
  """Split bytecode into blocks and order the blocks.

  This builds an "ancestor first" ordering of the basic blocks of the bytecode.

  Args:
    bytecode: A list of instances of opcodes.Opcode. (E.g. returned from
      opcodes.dis())

  Returns:
    A list of Block instances.
  """
  blocks = _split_bytecode(bytecode)
  first_op_to_block = {block.code[0]: block for block in blocks}
  for i, block in enumerate(blocks):
    next_block = blocks[i + 1] if i < len(blocks) - 1 else None
    last_op = block.code[-1]
    if next_block and not last_op.no_next():
      block.connect_outgoing(next_block)
    if last_op.target:
      block.connect_outgoing(first_op_to_block[last_op.target])
    if last_op.block_target:
      block.connect_outgoing(first_op_to_block[last_op.block_target])
  return cfg_utils.order_nodes(blocks)


class DisCodeVisitor(object):
  """Visitor for disassembling code into Opcode objects."""

  def visit_code(self, code):
    code.co_code = opcodes.dis_code(code)
    return code


def order_code(code):
  """Split a CodeType object into ordered blocks.

  This takes a CodeType object (i.e., a piece of compiled Python code) and
  splits it into ordered basic blocks.

  Args:
    code: A loadmarshal.CodeType object.

  Returns:
    A CodeBlocks instance.
  """
  bytecodes = code.co_code
  add_pop_block_targets(bytecodes)  # TODO(kramm): move into pyc/opcodes.py?
  return OrderedCode(code, bytecodes, compute_order(bytecodes),
                     code.python_version)


class OrderCodeVisitor(object):
  """Visitor for recursively changing all CodeType to OrderedCode.

  Depends on DisCodeVisitor having been run first.
  """

  def visit_code(self, code):
    return order_code(code)


class CollectTypeCommentTargetsVisitor(object):
  """Collect opcodes that might have type comments attached.

  Depends on DisCodeVisitor having been run first.
  """

  def __init__(self):
    # A mutable map of line: opcode for STORE_* opcodes. This is modified as the
    # visitor runs, and contains the last opcode for each line.
    self.store_op_map = collections.defaultdict(None)

  def visit_code(self, code):
    # For type comments attached to multi-opcode lines, we want to mark the
    # latest 'store' opcode and attach the type comment to it.
    for op in code.co_code:
      if isinstance(op, STORE_OPCODES):
        self.store_op_map[op.line] = op
    return code


class CollectFunctionTypeCommentTargetsVisitor(object):
  """Collect opcodes that might have function type comments attached.

  Depends on DisCodeVisitor having been run first.
  """

  def __init__(self):
    # A mutable list of (start, end, opcode) for MAKE_FUNCTION opcodes. This is
    # modified as the visitor runs, and contains the range of lines that could
    # contain function type comments.
    self.make_function_ops = []

  def visit_code(self, code):
    """Find MAKE_FUNCTION opcodes and record the comment line range."""
    # Offset between function code and MAKE_FUNCTION
    if code.python_version < (3, 4):
      # [LOAD_CONST <code>, MAKE_FUNCTION]
      offset = 1
    else:
      # [LOAD_CONST <code>, LOAD_CONST name, MAKE_FUNCTION]
      offset = 2
    for i, op in enumerate(code.co_code):
      if isinstance(op, opcodes.MAKE_FUNCTION):
        code_op = code.co_code[i - offset]
        assert isinstance(code_op, CODE_LOADING_OPCODES)
        fn_code = code.co_consts[code_op.arg]
        assert isinstance(fn_code, pyc.loadmarshal.CodeType)
        end_line = fn_code.co_code[0].line  # First line of code in body.
        self.make_function_ops.append((op.line + 1, end_line, op))
    return code


def merge_type_comments(code, type_comments):
  """Merges type comments into their associated opcodes.

  Modifies code in place.

  Args:
    code: CodeType object that has been disassembled (see DisCodeVisitor).
    type_comments: Map of type comments from the director.

  Returns:
    The code with type comments added to the relevant opcodes.
  """
  # Apply type comments to the STORE_* opcodes
  visitor = CollectTypeCommentTargetsVisitor()
  code = pyc.visit(code, visitor)
  for line, op in visitor.store_op_map.items():
    if line in type_comments:
      _, comment = type_comments[line]
      op.type_comment = comment

  # Apply type comments to the MAKE_FUNCTION opcodes
  visitor = CollectFunctionTypeCommentTargetsVisitor()
  code = pyc.visit(code, visitor)
  for start, end, op in visitor.make_function_ops:
    for i in range(start, end):
      # Take the first comment we find as the function typecomment.
      if i in type_comments:
        _, comment = type_comments[i]
        # Record the line number of the comment for error messages.
        op.type_comment = (comment, i)
        break
  return code


def process_code(code, type_comments):
  code = pyc.visit(code, DisCodeVisitor())
  code = merge_type_comments(code, type_comments)
  return pyc.visit(code, OrderCodeVisitor())
