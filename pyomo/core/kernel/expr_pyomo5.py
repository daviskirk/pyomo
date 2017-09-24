#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright 2017 National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and 
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain 
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

from __future__ import division

import math
import logging
import sys
import traceback
from copy import deepcopy

logger = logging.getLogger('pyomo.core')

from six import StringIO, next, string_types, itervalues
from six.moves import xrange, builtins
from weakref import ref

from pyutilib.math.util import isclose

from pyomo.core.kernel.numvalue import \
    (NumericValue,
     NumericConstant,
     native_types,
     native_numeric_types,
     as_numeric,
     value)
from pyomo.core.kernel.expr_common import \
    (_add, _sub, _mul, _div,
     _pow, _neg, _abs, _inplace,
     _unary, _radd, _rsub, _rmul,
     _rdiv, _rpow, _iadd, _isub,
     _imul, _idiv, _ipow, _lt, _le,
     _eq, 
     chainedInequalityErrorMessage as cIEM)
from pyomo.core.kernel import expr_common as common
from pyomo.core.base.param import _ParamData, SimpleParam
from pyomo.core.base.template_expr import TemplateExpressionError

##
## NEEDS TO BE REMOVED
##

def _clear_expression_pool():
    pass

sum = builtins.sum
_getrefcount_available = False

UNREFERENCED_EXPR_COUNT = 11
UNREFERENCED_INTRINSIC_EXPR_COUNT = -2
UNREFERENCED_EXPR_IF_COUNT = -3
if sys.version_info[:2] >= (3, 6):
    UNREFERENCED_EXPR_COUNT -= 1
    UNREFERENCED_INTRINSIC_EXPR_COUNT += 1
    UNREFERENCED_EXPR_IF_COUNT += 2
elif sys.version_info[:2] < (2, 7):
    UNREFERENCED_EXPR_IF_COUNT = -4

# Wrap the common chainedInequalityErrorMessage to pass the local context
chainedInequalityErrorMessage \
    = lambda *x: cIEM(generate_relational_expression, *x)

class EntangledExpressionError(Exception):
    def __init__(self, sub_expr):
        msg = \
"""Attempting to form an expression with a
subexpression that is already part of another expression of component.
This would create two expressions that share common subexpressions,
which is not allowed in Pyomo.  Either clone the subexpression using
'clone_expression' before creating the new expression, or if you want
the two expressions to share a common subexpression, use an Expression
component to store the subexpression and use the subexpression in each
expression.  Common subexpression:\n\t%s""" % (str(sub_expr),)
        super(EntangledExpressionError, self).__init__(msg)

#-------------------------------------------------------
#
# Global Data
#
#-------------------------------------------------------

class clone_counter_context(object):
    _count = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self

    @property
    def count(self):
        return clone_counter_context._count

clone_counter = clone_counter_context()


class ignore_entangled_expressions(object):
    detangle = True

    def __enter__(self):
        ignore_entangled_expressions.detangle = False

    def __exit__(self, *args):
        ignore_entangled_expressions.detangle = True


class mutable_sum_context(object):

    def __enter__(self):
        self.e = _MultiSumExpression([0])
        return self.e

    def __exit__(self, *args):
        self.e.__class__ = _StaticMultiSumExpression

linear_expression = mutable_sum_context()


#-------------------------------------------------------
#
# Functions used to process expression trees
#
#-------------------------------------------------------

def compress_expression(expr, verbose=False, dive=False, multiprod=False):
    #
    # Only compress a true expression DAG
    #
    # Note: This does not try to optimize the compression to recognize
    #   subgraphs.
    #
    # Note: This uses a two-part stack.  The boolean indicates whether the
    #   parent should be cloned (because a child has been replaced), and the
    #   tuple represents the current context during the tree search.
    #
    if expr.__class__ in native_numeric_types or not expr.is_expression() or not expr._potentially_variable():
        return expr
    if expr.__class__ is _MultiSumExpression:
        expr.__class__ = _CompressedSumExpression
        return expr
    if expr.__class__ in pyomo5_multisum_types:
        return expr
    #
    # Only compress trees whose root is _SumExpression
    #
    # Note: This tacitly avoids compressing all trees
    # that are not potentially variable, since they have a
    # different class.
    #
    if not dive and \
       not (expr.__class__ is _SumExpression or expr.__class__ is _NPV_SumExpression or expr.__class__ is _Constant_SumExpression):
        return expr
    #
    # The stack starts with the current expression
    #
    _stack = [ False, (expr, expr._args, 0, len(expr._args), [])]
    #
    # Iterate until the stack is empty
    #
    # Note: 1 is faster than True for Python 2.x
    #
    while 1:
        #
        # Get the top of the stack
        #   _obj        Current expression object
        #   _argList    The arguments for this expression objet
        #   _idx        The current argument being considered
        #   _len        The number of arguments
        #
        _obj, _argList, _idx, _len, _result = _stack.pop()
        _clone = _stack.pop()
        if _clone and _stack:
            _stack[-2] = True
        if verbose: #pragma:nocover
            print("*"*10 + " POP  " + "*"*10)
        #
        # Iterate through the arguments
        #
        while _idx < _len:
            if verbose: #pragma:nocover
                print("-"*30)
                print(type(_obj))
                print(_obj)
                print(_argList)
                print(_idx)
                print(_len)
                print(_result)
                print(_clone)
                print("-"*30)

            _sub = _argList[_idx]
            _idx += 1
            if _sub.__class__ in native_numeric_types:
                #
                # Store a native or numeric object
                #
                _result.append( _sub )
            elif _sub.__class__ not in pyomo5_expression_types or \
                 _sub.__class__ in pyomo5_multisum_types or \
                 _sub.__class__ is _MultiProdExpression or \
                 not _sub._potentially_variable():
                _result.append( _sub )
            else:
                #
                # Push an expression onto the stack
                #
                if verbose: #pragma:nocover
                    print("*"*10 + " PUSH " + "*"*10)
                _stack.append( False )
                _stack.append( (_obj, _argList, _idx, _len, _result) )
                _obj                    = _sub
                _argList                = _sub._args
                _idx                    = 0
                _len                    = len(_argList)
                _result                 = []
                _clone                  = False
    
        if verbose: #pragma:nocover
            print("="*30)
            print(type(_obj))
            print(_obj)
            print(_argList)
            print(_idx)
            print(_len)
            print(_result)
            print(_clone)
            print("="*30)
        #
        # Now replace the current expression object if it's a sum
        #
        if _obj.__class__ is _SumExpression or _obj.__class__ is _NPV_SumExpression or _obj.__class__ is _Constant_SumExpression:
            ans = _SumExpression._combine_expr(*_result)
            if _stack:
                #
                # We've replaced a node, so set the context for the parent's search to
                # ensure that it is cloned.
                #
                _stack[-2] = True
        #
        # Now replace the current expression object if it's a product
        #
        elif multiprod and _obj.__class__ in pyomo5_product_types:
            ans = _ProductExpression._combine_expr(*_result)
            if _stack:
                #
                # We've replaced a node, so set the context for the parent's search to
                # ensure that it is cloned.
                #
                _stack[-2] = True
        #
        # Now replace the current expression object if it's a reciprocal
        #
        elif multiprod and _obj.__class__ in pyomo5_reciprocal_types:
            ans = _ReciprocalExpression._combine_expr(*_result)
            if _stack:
                #
                # We've replaced a node, so set the context for the parent's search to
                # ensure that it is cloned.
                #
                _stack[-2] = True

        elif _clone:
            ans = _obj._clone( tuple(_result) )
            if _stack:
                _stack[-2] = True

        else:
            ans = _obj

        #print(ans)
        #print(ans._args)
        if verbose: #pragma:nocover
            print("STACK LEN %d" % len(_stack))
        if _stack:
            #
            # "return" the recursion by putting the return value on the end of the results stack
            #
            _stack[-1][-1].append( ans )
        else:
            if ans.__class__ is _MultiSumExpression:
                ans.__class__ = _CompressedSumExpression
            return ans


def clone_expression(expr, substitute=None, verbose=False, clone_leaves=True):
    from pyomo.core.kernel.numvalue import native_numeric_types
    #
    clone_counter_context._count += 1
    memo = {'__block_scope__': { id(None): False }}
    if substitute:
        memo.update(substitute)
    #
    if expr.__class__ in native_numeric_types:
        return expr
    if not expr.is_expression():
        return deepcopy(expr, memo)
    #
    # The stack starts with the current expression
    #
    _stack = [ (expr, expr._args, 0, len(expr._args), [])]
    #
    # Iterate until the stack is empty
    #
    # Note: 1 is faster than True for Python 2.x
    #
    while 1:
        #
        # Get the top of the stack
        #   _obj        Current expression object
        #   _argList    The arguments for this expression objet
        #   _idx        The current argument being considered
        #   _len        The number of arguments
        #
        _obj, _argList, _idx, _len, _result = _stack.pop()
        if verbose: #pragma:nocover
            print("*"*10 + " POP  " + "*"*10)
        #
        # Iterate through the arguments
        #
        while _idx < _len:
            if verbose: #pragma:nocover
                print("-"*30)
                print(type(_obj))
                print(_obj)
                print(_argList)
                print(_idx)
                print(_len)
                print(_result)

            _sub = _argList[_idx]
            _idx += 1
            if _sub.__class__ in native_numeric_types:
                #
                # Store a native or numeric object
                #
                _result.append( deepcopy(_sub, memo) )
            elif _sub.__class__ not in pyomo5_expression_types:
                #
                # Store a kernel object that is cloned
                #
                if clone_leaves:
                    _result.append( deepcopy(_sub, memo) )
                else:
                    _result.append( _sub )
            else:
                #
                # Push an expression onto the stack
                #
                if verbose: #pragma:nocover
                    print("*"*10 + " PUSH " + "*"*10)
                _stack.append( (_obj, _argList, _idx, _len, _result) )
                _obj     = _sub
                _argList = _sub._args
                _idx     = 0
                _len     = len(_argList)
                _result  = []
    
        if verbose: #pragma:nocover
            print("="*30)
            print(type(_obj))
            print(_obj)
            print(_argList)
            print(_idx)
            print(_len)
            print(_result)
        #
        # Now replace the current expression object
        #
        ans = _obj._clone( tuple(_result) )
        if verbose: #pragma:nocover
            print("STACK LEN %d" % len(_stack))
        if _stack:
            #
            # "return" the recursion by putting the return value on the end of the reults stack
            #
            _stack[-1][-1].append( ans )
        else:
            return ans


def _expression_size(expr, verbose=False):
    from pyomo.core.kernel.numvalue import native_numeric_types
    #
    # Note: This does not try to optimize the compression to recognize
    #   subgraphs.
    #
    if expr.__class__ in native_numeric_types or not expr.is_expression():
        return 1
    #
    # The stack starts with the current expression
    #
    _stack = [ (expr, expr._args, 0, len(expr._args), [])]
    #
    # Iterate until the stack is empty
    #
    # Note: 1 is faster than True for Python 2.x
    #
    while 1:
        #
        # Get the top of the stack
        #   _obj        Current expression object
        #   _argList    The arguments for this expression objet
        #   _idx        The current argument being considered
        #   _len        The number of arguments
        #
        _obj, _argList, _idx, _len, _result = _stack.pop()
        if verbose: #pragma:nocover
            print("*"*10 + " POP  " + "*"*10)
        #
        # Iterate through the arguments
        #
        while _idx < _len:
            if verbose: #pragma:nocover
                print("-"*30)
                print(type(_obj))
                print(_obj)
                print(_argList)
                print(_idx)
                print(_len)
                print(_result)

            _sub = _argList[_idx]
            _idx += 1
            if _sub.__class__ in native_numeric_types or not _sub.is_expression():
                #
                # Store a native or numeric object
                #
                _result.append( 1 )
            else:
                #
                # Push an expression onto the stack
                #
                if verbose: #pragma:nocover
                    print("*"*10 + " PUSH " + "*"*10)
                _stack.append( (_obj, _argList, _idx, _len, _result) )
                _obj     = _sub
                _argList = _sub._args
                _idx     = 0
                _len     = len(_argList)
                _result  = []
    
        if verbose: #pragma:nocover
            print("="*30)
            print(type(_obj))
            print(_obj)
            print(_argList)
            print(_idx)
            print(_len)
            print(_result)
            print("STACK LEN %d" % len(_stack))

        ans = sum(_result)+1
        if _stack:
            #
            # "return" the recursion by putting the return value on the end of the reults stack
            #
            _stack[-1][-1].append( ans )
        else:
            return ans


def evaluate_expression(exp, exception=True, only_fixed_vars=False):
    from pyomo.core.base import _VarData, _GeneralVarData, SimpleVar
    from pyomo.core.kernel.component_variable import IVariable, variable
    pyomo5_variable_types = set([_VarData, _GeneralVarData, IVariable, variable, SimpleVar])

    try:
        if exp.__class__ in pyomo5_variable_types:
            if not only_fixed_vars or exp.fixed:
                return exp.value
            else:
                raise ValueError("Cannot evaluate an unfixed variable with only_fixed_vars=True")
        elif exp.__class__ in native_numeric_types:
            return exp
        elif not exp.is_expression():
            return exp()

        _stack = [ (exp, exp._args, 0, len(exp._args), []) ]
        while 1:  # Note: 1 is faster than True for Python 2.x
            _obj, _argList, _idx, _len, _result = _stack.pop()
            while _idx < _len:
                _sub = _argList[_idx]
                _idx += 1
                if _sub.__class__ in native_numeric_types:
                    _result.append( _sub )
                elif _sub.is_expression():
                    _stack.append( (_obj, _argList, _idx, _len, _result) )
                    _obj     = _sub
                    _argList = _sub._args
                    _idx     = 0
                    _len     = len(_argList)
                    _result  = []
                elif _sub.__class__ in pyomo5_variable_types:
                    if only_fixed_vars:
                        if _sub.fixed:
                            _result.append( _sub.value )
                        else:
                            raise ValueError("Cannot evaluate an unfixed variable with only_fixed_vars=True")
                    else:
                        _result.append( value(_sub) )
                else:
                    _result.append( value(_sub) )
            ans = _obj._apply_operation(_result)
            if _stack:
                _stack[-1][-1].append( ans )
            else:
                return ans
    except TemplateExpressionError:
        if exception:
            raise
        return None
    except ValueError:
        if exception:
            raise
        return None


def identify_variables(expr,
                       include_fixed=True,
                       allow_duplicates=False,
                       include_potentially_variable=False):
    from pyomo.core.base import _VarData, _GeneralVarData, SimpleVar
    from pyomo.core.kernel.component_variable import IVariable, variable
    pyomo5_variable_types = set([_VarData, _GeneralVarData, IVariable, variable, SimpleVar])

    if not allow_duplicates:
        _seen = set()
    _stack = [ ([expr], 0, 1) ]
    while _stack:
        _argList, _idx, _len = _stack.pop()
        while _idx < _len:
            _sub = _argList[_idx]
            _idx += 1
            if _sub.__class__ in native_types:
                pass
            elif _sub.is_expression():
                _stack.append(( _argList, _idx, _len ))
                _argList = _sub._args
                _idx = 0
                _len = len(_argList)
            elif _sub.__class__ in pyomo5_variable_types:
                if ( include_fixed
                     or not _sub.is_fixed()
                     or include_potentially_variable ):
                    if not allow_duplicates:
                        if id(_sub) in _seen:
                            continue
                        _seen.add(id(_sub))
                    yield _sub
            elif include_potentially_variable and _sub._potentially_variable():
                if not allow_duplicates:
                    if id(_sub) in _seen:
                        continue
                    _seen.add(id(_sub))
                yield _sub


#-------------------------------------------------------
#
# Expression classes
#
#-------------------------------------------------------


class _ExpressionBase(NumericValue):
    """
    An object that defines a mathematical expression that can be evaluated

    m.p = Param(default=10, mutable=False)
    m.q = Param(default=10, mutable=True)
    m.x = var()
    m.y = var(initialize=1)
    m.y.fixed = True

                            m.p     m.q     m.x     m.y
    constant                T       F       F       F
    potentially_variable    F       F       T       T
    npv                     T       T       F       F
    fixed                   T       T       F       T
    """

    __slots__ =  ('_args','_owned')
    PRECEDENCE = 0

    def __init__(self, args):
        self._args = args
        self._owned = False
        for arg in args:
            if arg.__class__ in pyomo5_expression_types:
                arg._owned = True

    def __getstate__(self):
        state = super(_ExpressionBase, self).__getstate__()
        for i in _ExpressionBase.__slots__:
           state[i] = getattr(self,i)
        return state

    def __nonzero__(self):
        return bool(self())

    __bool__ = __nonzero__

    def __str__(self):
        from pyomo.repn import generate_standard_repn
        try:
            #
            # Try to factor the constant and linear terms when printing NONVERBOSE
            #
            if common.TO_STRING_VERBOSE:
                expr = self
            elif self.__class__ is _InequalityExpression:
                expr = self
                # TODO: chained inequalities
                #if self._args[0].__class__ is _InequalityExpression:
                #    repn0a = generate_standard_repn(self._args[0]._args[0], compress=False, quadratic=False, compute_values=False)
                #    repn0b = generate_standard_repn(self._args[0]._args[1], compress=False, quadratic=False, compute_values=False)
                #    lhs = _InequalityExpression( (repn0a.to_expression(), repn0b.to_expression()), self._args[0]._strict, self._args[0]._cloned_from)
                #    repn1 = generate_standard_repn(self._args[1], compress=False, quadratic=False, compute_values=False)
                #    expr = _InequalityExpression( (lhs, repn1.to_expression()), self._strict, self._cloned_from)
                #elif self._args[0].__class__ is _InequalityExpression:
                #    repn0 = generate_standard_repn(self._args[0], compress=False, quadratic=False, compute_values=False)
                #    repn1a = generate_standard_repn(self._args[1]._args[0], compress=False, quadratic=False, compute_values=False)
                #    repn1b = generate_standard_repn(self._args[1]._args[1], compress=False, quadratic=False, compute_values=False)
                #    rhs = _InequalityExpression( (repn1a.to_expression(), repn1b.to_expression()), self._args[1]._strict, self._args[1]._cloned_from)
                #    expr = _InequalityExpression( (repn0.to_expression(), rhs), self._strict, self._cloned_from)
                #else:
                #    repn0 = generate_standard_repn(self._args[0], compress=False, quadratic=False, compute_values=False)
                #    repn1 = generate_standard_repn(self._args[1], compress=False, quadratic=False, compute_values=False)
                #    expr = _InequalityExpression( (repn0.to_expression(), repn1.to_expression()), self._strict, self._cloned_from)
            elif self.__class__ is _EqualityExpression:
                repn0 = generate_standard_repn(self._args[0], quadratic=False, compute_values=False)
                repn1 = generate_standard_repn(self._args[1], quadratic=False, compute_values=False)
                expr = _EqualityExpression( (repn0.to_expression(), repn1.to_expression()) )
            else:
                repn = generate_standard_repn(self, quadratic=False, compute_values=False)
                expr = repn.to_expression()
        except Exception as e:
            print(str(e))
            #
            # Fall back to simply printing the expression in an
            # unfactored form.
            #
            expr = self
        #
        # Output the string
        #
        buf = StringIO()
        self.to_string(buf, expr=expr)
        ans = buf.getvalue()
        buf.close()
        return ans

    def __call__(self, exception=True):
        return evaluate_expression(self, exception)

    def clone(self, substitute=None, verbose=False):
        return clone_expression(self, substitute=None, verbose=verbose)

    def size(self, verbose=False):
        return _expression_size(self, verbose=verbose)

    def __deepcopy__(self, memo):
        return clone_expression(self, substitute=memo)

    def _clone(self, args):
        return self.__class__(args)

    def getname(self, *args, **kwds):
        """The text name of this Expression function"""
        raise NotImplementedError("Derived expression (%s) failed to "\
            "implement getname()" % ( str(self.__class__), ))

    # TODO: what if test was a lambda function?  Would that be faster?
    def _bool_tree_walker(self, test, combiner, native_result):
        _stack = []
        _combiner= getattr(self, combiner)()
        _argList = self._args
        _idx     = 0
        _len     = len(_argList)
        _result  = []
        while 1:  # Note: 1 is faster than True for Python 2.x
            while _idx < _len:
                _sub = _argList[_idx]
                _idx += 1
                if _sub.__class__ in native_numeric_types:
                    _result.append( native_result )
                    continue
                elif not _sub.__class__ in pyomo5_expression_types:
                    _result.append( getattr(_sub, test)() )
                    if _combiner is all:
                        if not _result[-1]:
                            _idx = _len
                    elif _combiner is any:
                        if _result[-1]:
                            _idx = _len
                else:
                    _stack.append( (_combiner, _argList, _idx, _len, _result) )
                    _combiner= getattr(_sub, combiner)()
                    _argList = _sub._args
                    _idx     = 0
                    _len     = len(_argList)
                    _result  = []

            ans = _combiner(_result)
            if _stack:
                _combiner, _argList, _idx, _len, _result = _stack.pop()
                _result.append( ans )
                if _combiner is all:
                    if not _result[-1]:
                        _idx = _len
                elif _combiner is any:
                    if _result[-1]:
                        _idx = _len
            else:
                return ans

    def is_constant(self):
        """Return True if this expression is an atomic constant

        This method contrasts with the is_fixed() method.  This method
        returns True if the expression is an atomic constant, that is it
        is composed exclusively of constants and immutable parameters.
        NumericValue objects returning is_constant() == True may be
        simplified to their numeric value at any point without warning.

        Note:  This defaults to False, but gets redefined in sub-classes.
        """
        return False

    def is_fixed(self):
        """Return True if this expression contains no free variables.

        The is_fixed() method returns True iff there are no free
        variables within this expression (i.e., all arguments are
        constants, params, and fixed variables).  The parameter values
        can of course change over time, but at any point in time, they
        are "fixed". hence, the name.

        """
        return self._bool_tree_walker('is_fixed', '_is_fixed_combiner', True)

    def _is_fixed_combiner(self):
        """Private method to be overridden by derived classes requiring special
        handling for computing is_fixed()

        This method should return a function that takes a list of the
        results of the is_fixed() for each of the arguments and
        returns True/False for this expression.

        """
        return all

    def _potentially_variable(self):
        """Return True if this expression can potentially contain a variable

        The potentially_variable() method returns True iff there are -
        or could be - any variables within this expression (i.e., at any
        point in the future, it is possible that is_fixed() might return
        False).

        Note:  This defaults to False, but gets redefined in sub-classes.

        TODO: Rename _potentially_variable() to potentially_variable()
        """
        return True

    def is_expression(self):
        return True

    def polynomial_degree(self):
        _stack = [ (self, self._args, 0, len(self._args), []) ]
        while 1:  # Note: 1 is faster than True for Python 2.x
            _obj, _argList, _idx, _len, _result = _stack.pop()
            while _idx < _len:
                _sub = _argList[_idx]
                _idx += 1
                if _sub.__class__ in native_numeric_types:
                    _result.append( 0 )
                elif _sub.is_expression():
                    _stack.append( (_obj, _argList, _idx, _len, _result) )
                    _obj     = _sub
                    _argList = _sub._args
                    _idx     = 0
                    _len     = len(_argList)
                    _result  = []
                else:
                    _result.append( 0 if _sub.is_fixed() else 1 )
            ans = _obj._polynomial_degree(_result)
            if _stack:
                _stack[-1][-1].append( ans )
            else:
                return ans

    def _polynomial_degree(self, ans):
        raise NotImplementedError("Derived expression (%s) failed to "\
            "implement _polynomial_degree()" % ( str(self.__class__), ))

    def to_string(self, ostream=None, verbose=None, precedence=None, expr=None):
        _name_buffer = {}
        if ostream is None:
            ostream = sys.stdout
        verbose = common.TO_STRING_VERBOSE if verbose is None else verbose

        if expr is None:
            expr = self
        _infix = False
        _bypass_prefix = False
        argList = expr._args
        _stack = [ [ expr, argList, 0, len(argList),
                     precedence if precedence is not None else expr._precedence() ] ]
        while _stack:
            _parent, _args, _idx, _len, _prec = _stack[-1]
            _my_precedence = _parent._precedence()
            if _idx < _len:
                _sub = _args[_idx]
                _stack[-1][2] += 1
                if _parent._to_string_skip(_idx):
                    continue
                if _infix:
                    _bypass_prefix = _parent._to_string_infix(ostream, _idx, verbose)
                else:
                    if not _bypass_prefix:
                        _parent._to_string_prefix(ostream, verbose)
                    else:
                        _bypass_prefix = False
                    if ((_len-_parent._to_string_skip(0) > 1) and _my_precedence > _prec) or not _my_precedence or verbose:
                        ostream.write("( ")
                        #ostream.write("%s %s %s %s %s %s( " % (str(_len), str(_idx), str(_my_precedence), str(_prec), str(verbose), str(type(_parent))))
                        #if _len == 2 and skip == 0:
                        #    ostream.write(" % ")
                        #    ostream.write(" %s " % str(_parent._to_string_skip(0)))
                        #    ostream.write(" % ")
                        #    ostream.write(str(_args[0]))
                        #    ostream.write(str(type(_args[0])))
                        #    ostream.write(" % ")
                        #    ostream.write(str(_args[1]))
                        #    ostream.write(str(type(_args[1])))
                        #    ostream.write(" % ")
                    _infix = True
                if _sub.__class__ in pyomo5_expression_types:
                #if hasattr(_sub, '_args'): # _args is a proxy for Expression
                    argList = _sub._args
                    _stack.append([ _sub, argList, 0, len(argList), _my_precedence ])
                    _infix = False
                elif hasattr(_parent, '_to_string_term'):
                    _parent._to_string_term(ostream, _idx, _sub, _name_buffer, verbose)
                else:
                    expr._to_string_term(ostream, _idx, _sub, _name_buffer, verbose)
            else:
                _parent._to_string_suffix(ostream, verbose)
                _stack.pop()
                if ((_len-_parent._to_string_skip(0) > 1) and _my_precedence > _prec) or not _my_precedence or verbose:
                    ostream.write(" )")

    def _precedence(self):
        return _ExpressionBase.PRECEDENCE

    def _to_string_skip(self, _idx):
        return False

    def _to_string_term(self, ostream, _idx, _sub, _name_buffer, verbose):
        if _sub.__class__ in native_numeric_types:
            ostream.write(str(_sub))
        elif _sub.__class__ is NumericConstant:
            ostream.write(str(_sub()))
        elif hasattr(_sub, 'to_string'):
             #
             # Generate strings from components that contain expressions, but
             # don't just generate the component name.
             #
            _sub.to_string(ostream=ostream, verbose=verbose)
        elif hasattr(_sub, 'getname'):
            # BUG?  The kernel may return None from getname()
            _s = _sub.getname(True, _name_buffer)
            if _s is None:
                _s = str(_sub)
            ostream.write(_s)
        else:
            ostream.write(str(_sub))

    def _to_string_prefix(self, ostream, verbose):
        if verbose:
            ostream.write(self.getname())

    def _to_string_infix(self, ostream, idx, verbose):
        if verbose:
            ostream.write(" , ")
        else:
            ostream.write(self._inline_operator())

    def _to_string_suffix(self, ostream, verbose):
        pass


class _NegationExpression(_ExpressionBase):
    __slots__ = ()

    PRECEDENCE = 4

    def getname(self, *args, **kwds):
        return 'neg'

    def _polynomial_degree(self, result):
        return result[0]

    def _precedence(self):
        return _NegationExpression.PRECEDENCE

    def _to_string_prefix(self, ostream, verbose):
        if verbose:
            ostream.write(self.getname())
        elif not self._args[0].is_expression \
             and _NegationExpression.PRECEDENCE <= self._args[0]._precedence():
            ostream.write("-")
        else:
            ostream.write("- ")

    def _apply_operation(self, result):
        return -result[0]


class _Constant_NegationExpression(_NegationExpression):
    __slots__ = ()

    def is_constant(self):
        return True

    def _potentially_variable(self):
        return False


class _NPV_NegationExpression(_NegationExpression):
    __slots__ = ()

    def _potentially_variable(self):
        return False


class _ExternalFunctionExpression(_ExpressionBase):
    __slots__ = ('_fcn',)

    def __init__(self, args, fcn=None):
        """Construct a call to an external function"""
        self._args = args
        self._fcn = fcn
        self._owned = False
        for arg in args:
            if arg.__class__ in pyomo5_expression_types:
                arg._owned = True

    def _clone(self, args):
        return self.__class__(args, self._fcn)

    def __getstate__(self):
        result = super(_ExternalFunctionExpression, self).__getstate__()
        for i in _ExternalFunctionExpression.__slots__:
            result[i] = getattr(self, i)
        return result

    def getname(self, *args, **kwds):
        return self._fcn.getname(*args, **kwds)

    def _polynomial_degree(self, result):
        if isclose(result[0], 0):
            return 0
        else:
            return None

    def _apply_operation(self, result):
        """Evaluate the expression"""
        return self._fcn.evaluate( result )

    def _inline_operator(self):
        return ', '


class _Constant_ExternalFunctionExpression(_ExternalFunctionExpression):
    __slots__ = ()

    def is_constant(self):
        return True

    def _potentially_variable(self):
        return False


class _NPV_ExternalFunctionExpression(_ExternalFunctionExpression):
    __slots__ = ()

    def _potentially_variable(self):
        return False


class _PowExpression(_ExpressionBase):

    __slots__ = ()
    PRECEDENCE = 2

    def _polynomial_degree(self, result):
        # _PowExpression is a tricky thing.  In general, a**b is
        # nonpolynomial, however, if b == 0, it is a constant
        # expression, and if a is polynomial and b is a positive
        # integer, it is also polynomial.  While we would like to just
        # call this a non-polynomial expression, these exceptions occur
        # too frequently (and in particular, a**2)
        l,r = result
        if isclose(r, 0):
            if isclose(l, 0):
                return 0
            try:
                # NOTE: use value before int() so that we don't
                #       run into the disabled __int__ method on
                #       NumericValue
                exp = value(self._args[1])
                if exp == int(exp):
                    if l is not None and exp > 0:
                        return l * exp
                    elif exp == 0:
                        return 0
            except:
                pass
        return None

    def _is_constant_combiner(self):
        def impl(args):
            if not args[1]:
                return False
            return args[0] or isclose(value(self._args[1]), 0)
        return impl

    # the local _is_fixed_combiner override is identical to
    # _is_constant_combiner:
    _is_fixed_combiner = _is_constant_combiner

    def _precedence(self):
        return _PowExpression.PRECEDENCE

    def _apply_operation(self, result):
        _l, _r = result
        return _l ** _r

    def getname(self, *args, **kwds):
        return 'pow'

    def _inline_operator(self):
        return '**'


class _Constant_PowExpression(_PowExpression):
    __slots__ = ()

    def is_constant(self):
        return True

    def _potentially_variable(self):
        return False


class _NPV_PowExpression(_PowExpression):
    __slots__ = ()

    def _potentially_variable(self):
        return False


class _LinearOperatorExpression(_ExpressionBase):
    """An 'abstract' class that defines the polynomial degree for a simple
    linear operator
    """

    __slots__ = ()

    def _polynomial_degree(self, result):
        # NB: We can't use max() here because None (non-polynomial)
        # overrides a numeric value (and max() just ignores it)
        ans = 0
        for x in result:
            if x is None:
                return None
            elif ans < x:
                ans = x
        return ans


class _InequalityExpression(_LinearOperatorExpression):
    """An object that defines a series of less-than or
    less-than-or-equal expressions"""

    __slots__ = ('_strict', '_cloned_from')
    PRECEDENCE = 9

    def __init__(self, args, strict, cloned_from):
        """Constructor"""
        super(_InequalityExpression,self).__init__(args)
        self._strict = strict
        self._cloned_from = cloned_from

    def _clone(self, args):
        return self.__class__(args, self._strict, self._cloned_from)

    def __getstate__(self):
        result = super(_InequalityExpression, self).__getstate__()
        for i in _InequalityExpression.__slots__:
            result[i] = getattr(self, i)
        return result

    def __nonzero__(self):
        if generate_relational_expression.chainedInequality is not None:
            raise TypeError(chainedInequalityErrorMessage())
        if not self.is_constant() and len(self._args) == 2:
            generate_relational_expression.call_info \
                = traceback.extract_stack(limit=2)[-2]
            generate_relational_expression.chainedInequality = self
            #return bool(self())                - This is needed to apply simple evaluation of inequalities
            return True

        return bool(self())

    __bool__ = __nonzero__

    def is_relational(self):
        return True

    def _precedence(self):
        return _InequalityExpression.PRECEDENCE

    def _apply_operation(self, result):
        for i, a in enumerate(result):
            if not i:
                pass
            elif self._strict[i-1]:
                if not _l < a:
                    return False
            else:
                if not _l <= a:
                    return False
            _l = a
        return True

    def _to_string_prefix(self, ostream, verbose):
        pass

    def _to_string_infix(self, ostream, idx, verbose):
        ostream.write( '  <  ' if self._strict[idx-1] else '  <=  ' )

    def is_constant(self):
        return self._args[0].is_constant() and self._args[1].is_constant()

    def _potentially_variable(self):
        return self._args[0]._potentially_variable() or self._args[1]._potentially_variable()


class _EqualityExpression(_LinearOperatorExpression):
    """An object that defines a equal-to expression"""

    __slots__ = ()
    PRECEDENCE = 9

    def __nonzero__(self):
        if generate_relational_expression.chainedInequality is not None:
            raise TypeError(chainedInequalityErrorMessage())
        return bool(self())

    __bool__ = __nonzero__

    def is_relational(self):
        return True

    def _precedence(self):
        return _EqualityExpression.PRECEDENCE

    def _apply_operation(self, result):
        _l, _r = result
        return _l == _r

    def _to_string_prefix(self, ostream, verbose):
        pass

    def _to_string_infix(self, ostream, idx, verbose):
        ostream.write('  ==  ' )

    def is_constant(self):
        return self._args[0].is_constant() and self._args[1].is_constant()

    def _potentially_variable(self):
        return self._args[0]._potentially_variable() or self._args[1]._potentially_variable()


class _ProductExpression(_ExpressionBase):
    """An object that defines a product expression"""

    __slots__ = ()
    PRECEDENCE = 4

    def _precedence(self):
        return _ProductExpression.PRECEDENCE

    def _polynomial_degree(self, result):
        # NB: We can't use sum() here because None (non-polynomial)
        # overrides a numeric value (and sum() just ignores it - or
        # errors in py3k)
        a, b = result
        if a is None or b is None:
            return None
        else:
            return a + b

    def getname(self, *args, **kwds):
        return 'prod'

    def _inline_operator(self):
        return '*'

    def _apply_operation(self, result):
        _l, _r = result
        return _l * _r

    @staticmethod
    def _combine_expr(_l, _r):
        #
        # p * X
        #
        if _l.__class__ in native_numeric_types:
            if _r.__class__ is _MultiProdExpression:
                #
                # p * MultiProd
                #
                # Multiply the LHS to the first term of the multiprod
                #
                _r._args[0] *= _l
                ans = _r
            else:
                #
                # p * expr
                #
                ans = _MultiProdExpression([_l, _r], nnum=2)
        #
        # Augment the current multiprod (LHS)
        #
        elif _l.__class__ is _MultiProdExpression:
            #
            # MultiProd * 1
            # MultiProd * p
            #
            # Multiply the RHS to the first term of the multiprod
            #
            if not _r._potentially_variable():
                #print("H4")
                _l._args[0] *= _r
                ans = _l
            #
            # MultiProd * MultiProd
            #
            # Multiply the constant terms, and place the others
            #
            elif _r.__class__ is _MultiProdExpression:
                tmp = []
                tmp.append(_l._args[0] * _r._args[0])
                for i in range(1,_l._nnum):
                    tmp.append(_l._args[i])
                for i in range(1,_r._nnum):
                    tmp.append(_r._args[i])
                tmp += _l._args[_l._nnum:]
                tmp += _r._args[_r._nnum:]
                _l._args = tmp
                _l._nnum += _r._nnum-1
                ans = _l
            #
            # MultiProd * expr
            #
            # Insert the expression
            #
            else:
                #print("H5")
                if len(_l._args) == _l._nnum:
                    _l._args.append(_r)
                    _l._nnum += 1
                    ans = _l
                else:
                    tmp = _l._args[:_l._nnum]
                    tmp.append(_r)
                    tmp += _l._args[_l._nnum:]
                    _l._args = tmp
                    _l._nnum += 1
                    ans = _l
        #
        # p * X
        #
        elif not _l._potentially_variable():
            if _r.__class__ is _MultiProdExpression:
                #
                # p * MultiProd
                #
                # Multiply the LHS to the first term of the multiprod
                #
                _r._args[0] *= _l
                ans = _r
            else:
                #
                # p * expr
                #
                ans = _MultiProdExpression([_l, _r], nnum=2)
        #
        # Augment the current multiprod (RHS)
        #
        # WEH:  I'm not sure that this branch is possible with normal
        #       iteratively created products, but I still think it's 
        #       technically possible to create an expression tree that 
        #       has no products on the LHS and products on the RHS.
        #
        elif _r.__class__ is _MultiProdExpression:
            #
            # expr * MultiProd
            #
            # Insert the expression
            #
            #print(("H3",_r_clone))
            _r._args = [_r._args[0]] + [_l] + _r._args[1:]
            _r._nnum += 1
            ans = _r

        else:
            ans = _MultiProdExpression([1, _l, _r], nnum=3)
        return ans


class _Constant_ProductExpression(_ProductExpression):
    __slots__ = ()

    def is_constant(self):
        return True

    def _potentially_variable(self):
        return False


class _NPV_ProductExpression(_ProductExpression):
    __slots__ = ()

    def _potentially_variable(self):
        return False


class _MultiProdExpression(_ProductExpression):
    """An object that defines a product with 1 or more terms, including denominators."""

    __slots__ = ('_nnum')
    PRECEDENCE = 4

    def __init__(self, args, nnum=None):
        self._args = args
        self._nnum = nnum
        self._owned = False
        for arg in args:
            if arg.__class__ in pyomo5_expression_types:
                arg._owned = True

    def _clone(self, args):
        return self.__class__(args, self._nnum)

    def _precedence(self):
        return _MultiProdExpression.PRECEDENCE

    def _apply_operation(self, result):
        return prod(result)

    def getname(self, *args, **kwds):
        return 'multiprod'

    def _potentially_variable(self):
        return len(self._args) > 1

    def _apply_operation(self, result):
        ans = 1
        i = 0
        n_ = len(self._args)
        for j in xargs(0,nnum):
            ans *= result[i]
            i += 1
        while i < n_:
            ans /= result[i]
            i += 1


class _ReciprocalExpression(_ExpressionBase):
    """An object that defines a division expression"""

    __slots__ = ()
    PRECEDENCE = 3.5

    def _precedence(self):
        return _ReciprocalExpression.PRECEDENCE

    def _polynomial_degree(self, result):
        if isclose(result[0], 0):
            return 0
        return None

    def getname(self, *args, **kwds):
        return 'recip'

    def _to_string_prefix(self, ostream, verbose):
        ostream.write("(1/")

    def _to_string_suffix(self, ostream, verbose):
        ostream.write(")")

    def _apply_operation(self, result):
        return 1 / result[0]

    @staticmethod
    def _combine_expr(_r):
        _l = 1
        #
        # 1 / X
        #
        if _r.__class__ is _MultiProdExpression:
            #
            # 1 / MultiProd
            #
            # Reciprocate the MultiProd
            #
            _tmp = [1/_r._args[0]] + _r._args[_r._nnum:]
            for i in range(1,_r._nnum):
                _tmp.append( _r._args[i])
            _r._args = _tmp
            _r._nnum = len(_tmp)-_r._nnum+1
            ans = _r
        else:
            #
            # 1 / expr
            #
            ans = _MultiProdExpression([_l, _r], nnum=1)
        return ans


class _Constant_ReciprocalExpression(_ReciprocalExpression):
    __slots__ = ()

    def is_constant(self):
        return True

    def _potentially_variable(self):
        return False


class _NPV_ReciprocalExpression(_ReciprocalExpression):
    __slots__ = ()

    def _potentially_variable(self):
        return False


class _SumExpression(_LinearOperatorExpression):
    """An object that defines a simple summation of expressions"""

    __slots__ = ()
    PRECEDENCE = 6

    def _precedence(self):
        return _SumExpression.PRECEDENCE

    def _to_string_infix(self, ostream, idx, verbose):
        if verbose:
            ostream.write(" , ")
        else:
            if self._args[idx].__class__ is _NegationExpression:
                ostream.write(' - ')
                return True
            else:
                ostream.write(' + ')

    def _apply_operation(self, result):
        l_, r_ = result
        return l_ + r_

    def getname(self, *args, **kwds):
        return 'sum'

    #@profile
    @staticmethod
    def _combine_expr(_l, _r):
        #
        # Augment the current multi-sum (LHS)
        #
        if _l.__class__ is _MultiSumExpression:
            #
            # Multisum + 1
            # MultiSum + p
            #
            # Add the RHS to the first term of the multisum
            #
            if _r.__class__ in native_numeric_types or not _r._potentially_variable():
                _l._args[0] += _r
                ans = _l
            #
            # MultiSum + MultiSum
            #
            # Add the constant terms, and place the others
            #
            elif _r.__class__ is _MultiSumExpression:
                _l._args[0] += _r._args[0]
                _l._args += _r._args[1:]
                ans = _l
            #
            # MultiSum + StaticMultiSum
            #
            # Add the constant terms, and place the others
            #
            elif _r.__class__ is _CompressedSumExpression or _r.__class__ is _StaticMultiSumExpression:
                _l._args[0] += _r._args[0]
                _l._args += list(_r._args[1:])
                ans = _l
            #
            # Multisum + expr
            #
            # Insert the expression
            #
            else:
                #print("H5")
                _l._args.append(_r)
                ans = _l

        #
        # Augment the current multi-sum (RHS)
        #
        elif _r.__class__ is _MultiSumExpression:
            #
            # 1 + MultiSum
            # p + MultiSum
            #
            # Add the LHS to the first term of the multisum
            #
            if _l.__class__ in native_numeric_types or not _l._potentially_variable():
                _r._args[0] += _l
                ans = _r
            #
            # StaticMultiSum + MultiSum
            #
            # Add the constant terms, and place the others
            #
            elif _l.__class__ is _CompressedSumExpression or _l.__class__ is _StaticMultiSumExpression:
                _r._args[0] += _l._args[0]
                _r._args += list(_l._args[1:])
                ans = _r
            #
            # expr + MultiSum
            #
            # Insert the expression
            #
            else:
                _r._args.append(_l)
                ans = _r

        #
        # 1 + expr
        # p + expr
        #
        elif _l.__class__ in native_numeric_types or not _l._potentially_variable():
            ans = _MultiSumExpression([_l, _r])

        #
        # expr + 1
        # expr + p
        #
        elif _r.__class__ in native_numeric_types or not _r._potentially_variable():
            ans = _MultiSumExpression([_r, _l])

        #
        # expr + expr
        #
        else:
            ans = _MultiSumExpression([0, _l, _r])

        return ans


class _Constant_SumExpression(_SumExpression):
    __slots__ = ()

    def is_constant(self):
        return True

    def _potentially_variable(self):
        return False


class _NPV_SumExpression(_SumExpression):
    __slots__ = ()

    def _potentially_variable(self):
        return False


class _MultiSumExpression(_SumExpression):
    """An object that defines a summation with 1 or more terms and a constant term."""

    __slots__ = ()
    PRECEDENCE = 6

    def _precedence(self):
        return _MultiSumExpression.PRECEDENCE

    def _apply_operation(self, result):
        return sum(result)

    def getname(self, *args, **kwds):
        return 'multisum'

    def is_constant(self):
        return len(self._args) <= 1

    def _potentially_variable(self):
        return len(self._args) > 1

    def _to_string_skip(self, _idx):
        return  _idx == 0 and \
                self._args[0].__class__ in native_numeric_types and \
                isclose(self._args[0], 0)

    def X_to_string_infix(self, ostream, idx, verbose):
        if verbose:
            ostream.write(" , ")
        else:
            if self._args[idx].__class__ is _NegationExpression:
                ostream.write(' - ')
                return True
            else:
                ostream.write(' + ')


class _StaticMultiSumExpression(_MultiSumExpression):
    """A temporary object that defines a summation with 1 or more terms and a constant term."""
    
    __slots__ = ()


class _CompressedSumExpression(_MultiSumExpression):
    """A temporary object that defines a summation with 1 or more terms and a constant term."""
    
    __slots__ = ()


class _GetItemExpression(_ExpressionBase):
    """Expression to call "__getitem__" on the base"""

    __slots__ = ('_base',)
    PRECEDENCE = 1

    def _precedence(self):
        return _GetItemExpression.PRECEDENCE

    def __init__(self, args, base=None):
        """Construct an expression with an operation and a set of arguments"""
        self._args = args
        self._base = base
        self._owned = False
        for arg in args:
            if arg.__class__ in pyomo5_expression_types:
                arg._owned = True

    def _clone(self, args):
        return self.__class__(args, self._base)

    def __getstate__(self):
        result = super(_GetItemExpression, self).__getstate__()
        for i in _GetItemExpression.__slots__:
            result[i] = getattr(self, i)
        return result

    def getname(self, *args, **kwds):
        return self._base.getname(*args, **kwds)

    def _potentially_variable(self):
        if any(evaluate_expression(arg, exception=False) for arg in self._args):
            for x in itervalues(self._base):
                if not x.__class__ in native_types and x._potentially_variable():
                    return True
        return False
        
    def is_fixed(self):
        if any(self._args):
            for x in itervalues(self._base):
                if not x.__class__ in native_types and not x.is_fixed():
                    return False
        return True
        
    def _polynomial_degree(self, result):
        if any(x != 0 for x in result):
            return None
        ans = 0
        for x in itervalues(self._base):
            if x.__class__ in native_types:
                continue
            tmp = x.polynomial_degree()
            if tmp is None:
                return None
            elif tmp > ans:
                ans = tmp
        return ans

    def _apply_operation(self, result):
        return value(self._base.__getitem__( tuple(result) ))

    def _to_string_prefix(self, ostream, verbose):
        ostream.write(self.name)

    def resolve_template(self):
        return self._base.__getitem__(tuple(value(i) for i in self._args))


class Expr_if(_ExpressionBase):
    """An object that defines a dynamic if-then-else expression"""

    __slots__ = ('_if','_then','_else')

    # **NOTE**: This class evaluates the branching "_if" expression
    #           on a number of occasions. It is important that
    #           one uses __call__ for value() and NOT bool().

    def __init__(self, IF=None, THEN=None, ELSE=None):
        """Constructor"""
        if type(IF) is tuple and THEN==None and ELSE==None:
            IF, THEN, ELSE = IF
        self._args = (IF, THEN, ELSE)
        self._if = IF
        self._then = THEN
        self._else = ELSE
        if self._if.__class__ in native_types:
            self._if = as_numeric(self._if)
        self._owned = False
        if IF.__class__ in pyomo5_expression_types:
            IF._owned = True
        if THEN.__class__ in pyomo5_expression_types:
            THEN._owned = True
        if ELSE.__class__ in pyomo5_expression_types:
            ELSE._owned = True

    def __getstate__(self):
        state = super(Expr_if, self).__getstate__()
        for i in Expr_if.__slots__:
            state[i] = getattr(self, i)
        return state

    def getname(self, *args, **kwds):
        return "Expr_if"

    def _is_constant_combiner(self):
        def impl(args):
            if args[0]: #self._if.is_constant():
                if self._if():
                    return args[1] #self._then.is_constant()
                else:
                    return args[2] #self._else.is_constant()
            else:
                return False
        return impl

    # the local _is_fixed_combiner override is identical to
    # _is_constant_combiner:
    _is_fixed_combiner = _is_constant_combiner

    def is_constant(self):
        if self._if.__class__ in native_numeric_types or self._if.is_constant():
            if value(self._if):
                return (self._then.__class__ in native_numeric_types or self._then.is_constant())
            else:
                return (self._else.__class__ in native_numeric_types or self._else.is_constant())
        else:
            return (self._then.__class__ in native_numeric_types or self._then.is_constant()) and (self._else.__class__ in native_numeric_types or self._else.is_constant())

    def _potentially_variable(self):
        return (not self._if.__class__ in native_numeric_types and self._if._potentially_variable()) or (not self._then.__class__ in native_numeric_types and self._then._potentially_variable()) or (not self._if.__class__ in native_numeric_types and self._else._potentially_variable())

    def _polynomial_degree(self, result):
        _if, _then, _else = result
        if _if == 0:
            try:
                return _then if self._if() else _else
            except:
                pass
        return None

    def _to_string_term(self, ostream, _idx, _sub, _name_buffer, verbose):
        ostream.write("%s=( " % ('if','then','else')[_idx], )
        if type(self._args[_idx]) in native_numeric_types:
            ostream.write(str(self._args[_idx]))
        else:
            self._args[_idx].to_string(ostream=ostream, verbose=verbose)
        ostream.write(" )")

    def _to_string_prefix(self, ostream, verbose):
        ostream.write(self.getname())

    def _to_string_infix(self, ostream, idx, verbose):
        ostream.write(", ")

    def _apply_operation(self, result):
        _if, _then, _else = result
        return _then if _if else _else


class _UnaryFunctionExpression(_ExpressionBase):
    """An object that defines a mathematical expression that can be evaluated"""

    # TODO: Unary functions should define their own subclasses so as to
    # eliminate the need for the fcn and name slots
    __slots__ = ('_fcn', '_name')

    def __init__(self, args, name=None, fcn=None):
        """Construct an expression with an operation and a set of arguments"""
        if not type(args) is tuple:
            args = (args,)
        self._args = args
        self._name = name
        self._fcn = fcn
        self._owned = False
        if args[0].__class__ in pyomo5_expression_types:
            args[0]._owned = True

    def _clone(self, args):
        return self.__class__(args, self._name, self._fcn)

    def __getstate__(self):
        result = super(_UnaryFunctionExpression, self).__getstate__()
        for i in _UnaryFunctionExpression.__slots__:
            result[i] = getattr(self, i)
        return result

    def getname(self, *args, **kwds):
        return self._name

    def _to_string_prefix(self, ostream, verbose):
        ostream.write(self.getname())

    def _polynomial_degree(self, result):
        if isclose(result[0], 0):
            return 0
        else:
            return None

    def _apply_operation(self, result):
        return self._fcn(result[0])


class _Constant_UnaryFunctionExpression(_UnaryFunctionExpression):
    __slots__ = ()

    def is_constant(self):
        return True

    def _potentially_variable(self):
        return False


class _NPV_UnaryFunctionExpression(_UnaryFunctionExpression):
    __slots__ = ()

    def _potentially_variable(self):
        return False



# TODO: Should this actually be a special class, or just an instance of
#       _UnaryFunctionExpression (like sin, cos, etc)?
class _AbsExpression(_UnaryFunctionExpression):

    __slots__ = ()

    def __init__(self, arg):
        super(_AbsExpression, self).__init__(arg, 'abs', abs)

    def _clone(self, args):
        return self.__class__(args)


class _Constant_AbsExpression(_AbsExpression):
    __slots__ = ()

    def is_constant(self):
        return True

    def _potentially_variable(self):
        return False


class _NPV_AbsExpression(_AbsExpression):
    __slots__ = ()

    def _potentially_variable(self):
        return False


#-------------------------------------------------------
#
# Functions used to generate expressions
#
#-------------------------------------------------------

def _process_arg(obj):
    if obj.__class__ in native_types:
        return obj

    if obj.is_expression():
        if ignore_entangled_expressions.detangle and \
           obj.__class__ in pyomo5_expression_types and obj._owned:
            #
            # If the expression is owned, then we need to
            # clone it to avoid creating an entangled expression.
            #
            # But we don't have to worry about entanglement amongst non-expression
            # objects.
            #
            # Compress the expression before cloning, which 
            # should make cloning less expensive.
            #
            return clone_expression( compress_expression( obj ), clone_leaves=False )
        return obj

    if obj.__class__ is NumericConstant:
        return value(obj)

    if (obj.__class__ is _ParamData or obj.__class__ is SimpleParam) and not obj._component()._mutable:
        if not obj._constructed:
            return obj
        if obj.value is None:
            return obj
        return obj.value

    if obj.is_indexed():
        raise TypeError(
                "Argument for expression is an indexed numeric "
                "value\nspecified without an index:\n\t%s\nIs this "
                "value defined over an index that you did not specify?"
                % (obj.name, ) )

    return obj


#@profile
def generate_expression(etype, _self, _other):

    if etype > _inplace:
        etype -= _inplace

    if _self.__class__ is not _MultiSumExpression:
        _self = _process_arg(_self)

    if etype >= _unary:
        #
        # - x
        #
        if etype == _neg:
            if _self.__class__ in native_numeric_types:
                return - _self
            elif _self.is_constant():
                return _Constant_NegationExpression((_self,))
            elif _self._potentially_variable():
                return _NegationExpression((_self,))
            else:
                return _NPV_NegationExpression((_self,))
        #
        # abs(x)
        #
        elif etype == _abs:
            if _self.__class__ in native_numeric_types:
                return abs(_self)
            elif _self.is_constant():
                return _Constant_AbsExpression(_self)
            elif _self._potentially_variable():
                return _AbsExpression(_self)
            else:
                return _NPV_AbsExpression(_self)

        else: #pragma:nocover
            raise DeveloperError(
                "Unexpected unary operator id (%s)" % ( etype, ))

    if _self.__class__ is not _MultiSumExpression:
        _other = _process_arg(_other)

    if etype < 0:
        #
        # This may seem obvious, but if we are performing an
        # "R"-operation (i.e. reverse operation), then simply reverse
        # self and other.  This is legitimate as we are generating a
        # completely new expression here.
        #
        etype *= -1
        _self, _other = _other, _self

    if etype == _mul:
        #
        # x * y
        #
        if _other.__class__ in native_numeric_types:
            if _self.__class__ in native_numeric_types:
                return _self * _other
            elif _other == 0:   # isclose(_other, 0)
                return 0
            elif _other == 1:
                return _self
            if _self.is_constant():
                return _Constant_ProductExpression((_self, _other))
            elif _self._potentially_variable():
                return _ProductExpression((_other, _self))
            return _NPV_ProductExpression((_self, _other))
        elif _self.__class__ in native_numeric_types:
            if _self == 0:  # isclose(_self, 0)
                return 0
            elif _self == 1:
                return _other
            if _other.is_constant():
                return _Constant_ProductExpression((_self, _other))
            elif _other._potentially_variable():
                return _ProductExpression((_self, _other))
            return _NPV_ProductExpression((_self, _other))
        elif _other._potentially_variable():
            return _ProductExpression((_self, _other))
        elif _self._potentially_variable():
            return _ProductExpression((_other, _self))
        elif not _other.is_constant():
            return _NPV_ProductExpression((_self, _other))
        elif not _self.is_constant():
            return _NPV_ProductExpression((_self, _other))
        return _Constant_ProductExpression((_self, _other))

    elif etype == _add:
        #
        # x + y
        #
        if _self.__class__ is _MultiSumExpression or _other.__class__ is _MultiSumExpression:
            return _SumExpression._combine_expr(_self, _other)
        elif _other.__class__ in native_numeric_types:
            if _self.__class__ in native_numeric_types:
                return _self + _other
            elif _other == 0:   #isclose(_other, 0):
                return _self
            if _self.is_constant():
                return _Constant_SumExpression((_self, _other))
            elif _self._potentially_variable():
                return _SumExpression((_other, _self))
            return _NPV_SumExpression((_self, _other))
        elif _self.__class__ in native_numeric_types:
            if _self == 0:      #isclose(_self, 0):
                return _other
            if _other.is_constant():
                return _Constant_SumExpression((_self, _other))
            elif _other._potentially_variable():
                return _SumExpression((_self, _other))
            return _NPV_SumExpression((_self, _other))
        elif _other._potentially_variable():
            return _SumExpression((_self, _other))
        elif _self._potentially_variable():
            return _SumExpression((_other, _self))
        elif not _other.is_constant():
            return _NPV_SumExpression((_self, _other))
        elif not _self.is_constant():
            return _NPV_SumExpression((_self, _other))
        return _Constant_SumExpression((_self, _other))

    elif etype == _sub:
        #
        # x - y
        #
        if _other.__class__ in native_numeric_types:
            if _self.__class__ in native_numeric_types:
                return _self - _other
            elif isclose(_other, 0):
                return _self
            if _self.is_constant():
                return _Constant_SumExpression((-_other, _self))
            elif _self._potentially_variable():
                return _SumExpression((-_other, _self))
            return _NPV_SumExpression((-_other, _self))
        elif _self.__class__ in native_numeric_types:
            if isclose(_self, 0):
                if _other.is_constant():
                    return _Constant_NegationExpression((_other,))
                elif _other._potentially_variable():
                    return _NegationExpression((_other,))
                return _NPV_NegationExpression((_other,))
            if _other.is_constant():    
                return _Constant_SumExpression((_self, _Constant_NegationExpression((_other,))))
            elif _other._potentially_variable():    
                return _SumExpression((_self, _NegationExpression((_other,))))
            return _NPV_SumExpression((_self, _NPV_NegationExpression((_other,))))
        elif _other._potentially_variable():    
            return _SumExpression((_self, _NegationExpression((_other,))))
        elif _self._potentially_variable():
            return _SumExpression((_NPV_NegationExpression((_other,)), _self))
        elif not _other.is_constant():    
            return _NPV_SumExpression((_self, _NPV_NegationExpression((_other,))))
        elif not _self.is_constant():    
            return _NPV_SumExpression((_self, _Constant_NegationExpression((_other,))))
        else:
            return _Constant_SumExpression((_self, _Constant_NegationExpression((_other,))))
        
    elif etype == _div:
        #
        # x / y
        #
        if _other.__class__ in native_numeric_types:
            if _other == 1:
                return _self
            elif not _other:
                raise ZeroDivisionError()
            elif _self.__class__ in native_numeric_types:
                return _self / _other
            elif _self.is_constant():
                return _Constant_ProductExpression((1/_other, _self))
            elif _self._potentially_variable():
                return _ProductExpression((1/_other, _self))
            return _NPV_ProductExpression((1/_other, _self))
        elif _self.__class__ in native_numeric_types:
            if isclose(_self, 0):
                return 0
            elif _self == 1:
                if _other.is_constant():
                    return _Constant_ReciprocalExpression((_other,))
                elif _other._potentially_variable():
                    return _ReciprocalExpression((_other,))
                return _NPV_ReciprocalExpression((_other,))
            elif _other.is_constant():
                return _Constant_ProductExpression((_self, _Constant_ReciprocalExpression((_other,))))
            if _other._potentially_variable():
                return _ProductExpression((_self, _ReciprocalExpression((_other,))))
            return _NPV_ProductExpression((_self, _ReciprocalExpression((_other,))))
        elif _other._potentially_variable():
            return _ProductExpression((_self, _ReciprocalExpression((_other,))))
        elif _self._potentially_variable():
            return _ProductExpression((_self, _NPV_ReciprocalExpression((_other,))))
        elif not _other.is_constant():
            return _NPV_ProductExpression((_self, _NPV_ReciprocalExpression((_other,))))
        elif not _self.is_constant():
            return _NPV_ProductExpression((_self, _Constant_ReciprocalExpression((_other,))))
        return _Constant_ProductExpression((_self, _Constant_ReciprocalExpression((_other,))))

    elif etype == _pow:
        if _other.__class__ in native_numeric_types:
            if _other == 1:
                return _self
            elif not _other:
                return 1
            elif _self.__class__ in native_numeric_types:
                return _self ** _other
            elif _self.is_constant():
                return _Constant_PowExpression((_self, _other))
            elif _self._potentially_variable():
                return _PowExpression((_self, _other))
            return _NPV_PowExpression((_self, _other))
        elif _self.__class__ in native_numeric_types:
            if _other.is_constant():
                return _Constant_PowExpression((_self, _other))
            elif _other._potentially_variable():
                return _PowExpression((_self, _other))
            return _NPV_PowExpression((_self, _other))
        elif _self._potentially_variable() or _other._potentially_variable():
            return _PowExpression((_self, _other))
        elif not _self.is_constant() or not _other.is_constant():
            return _NPV_PowExpression((_self, _other))
        return _Constant_PowExpression((_self, _other))

    raise RuntimeError("Unknown expression type '%s'" % etype)



def generate_relational_expression(etype, lhs, rhs):
    # We cannot trust Python not to recycle ID's for temporary POD data
    # (e.g., floats).  So, if it is a "native" type, we will record the
    # value, otherwise we will record the ID.  The tuple for native
    # types is to guarantee that a native value will *never*
    # accidentally match an ID
    cloned_from = (
        id(lhs) if lhs.__class__ not in native_numeric_types else (0,lhs),
        id(rhs) if rhs.__class__ not in native_numeric_types else (0,rhs)
    )
    rhs_is_relational = False
    lhs_is_relational = False

    #
    # TODO: It would be nice to reduce all Constants to literals (and
    # not carry around the overhead of the NumericConstants). For
    # consistency, we will not do that yet, as many things downstream
    # would break; in particular within Constraint.add.  This way, all
    # arguments in the relational Expression's _args will be guaranteed
    # to be NumericValues (just as they are for all other Expressions).
    #
    lhs = _process_arg(lhs)
    rhs = _process_arg(rhs)

    if lhs.__class__ in native_numeric_types:
        lhs = as_numeric(lhs)
    elif lhs.is_relational():
        lhs_is_relational = True

    if rhs.__class__ in native_numeric_types:
        rhs = as_numeric(rhs)
    elif rhs.is_relational():
        rhs_is_relational = True

    if generate_relational_expression.chainedInequality is not None:
        prevExpr = generate_relational_expression.chainedInequality
        match = []
        # This is tricky because the expression could have been posed
        # with >= operators, so we must figure out which arguments
        # match.  One edge case is when the upper and lower bounds are
        # the same (implicit equality) - in which case *both* arguments
        # match, and this should be converted into an equality
        # expression.
        for i,arg in enumerate(prevExpr._cloned_from):
            if arg == cloned_from[0]:
                match.append((i,0))
            elif arg == cloned_from[1]:
                match.append((i,1))
        if etype == _eq:
            raise TypeError(chainedInequalityErrorMessage())
        if len(match) == 1:
            if match[0][0] == match[0][1]:
                raise TypeError(chainedInequalityErrorMessage(
                    "Attempting to form a compound inequality with two "
                    "%s bounds" % ('lower' if match[0][0] else 'upper',)))
            if not match[0][1]:
                cloned_from = prevExpr._cloned_from + (cloned_from[1],)
                lhs = prevExpr
                lhs_is_relational = True
            else:
                cloned_from = (cloned_from[0],) + prevExpr._cloned_from
                rhs = prevExpr
                rhs_is_relational = True
        elif len(match) == 2:
            # Special case: implicit equality constraint posed as a <= b <= a
            if prevExpr._strict[0] or etype == _lt:
                generate_relational_expression.chainedInequality = None
                buf = StringIO()
                prevExpr.to_string(buf)
                raise TypeError("Cannot create a compound inequality with "
                      "identical upper and lower\n\tbounds using strict "
                      "inequalities: constraint infeasible:\n\t%s and "
                      "%s < %s" % ( buf.getvalue().strip(), lhs, rhs ))
            if match[0] == (0,0):
                # This is a particularly weird case where someone
                # evaluates the *same* inequality twice in a row.  This
                # should always be an error (you can, for example, get
                # it with "0 <= a >= 0").
                raise TypeError(chainedInequalityErrorMessage())
            etype = _eq
        else:
            raise TypeError(chainedInequalityErrorMessage())
        generate_relational_expression.chainedInequality = None

    if etype == _eq:
        if lhs_is_relational or rhs_is_relational:
            buf = StringIO()
            if lhs_is_relational:
                lhs.to_string(buf)
            else:
                rhs.to_string(buf)
            raise TypeError("Cannot create an EqualityExpression where "\
                  "one of the sub-expressions is a relational expression:\n"\
                  "    " + buf.getvalue().strip())
        ans = _EqualityExpression((lhs,rhs))
        return ans
    else:
        if etype == _le:
            strict = (False,)
        elif etype == _lt:
            strict = (True,)
        else:
            raise ValueError("Unknown relational expression type '%s'" % etype)
        if lhs_is_relational:
            if lhs.__class__ is _InequalityExpression:
                if rhs_is_relational:
                    raise TypeError("Cannot create an InequalityExpression "\
                          "where both sub-expressions are also relational "\
                          "expressions (we support no more than 3 terms "\
                          "in an inequality expression).")
                if len(lhs._args) > 2:
                    raise ValueError("Cannot create an InequalityExpression "\
                          "with more than 3 terms.")
                lhs._args = lhs._args + (rhs,)
                lhs._strict = lhs._strict + strict
                lhs._cloned_from = cloned_from
                return lhs
            else:
                buf = StringIO()
                lhs.to_string(buf)
                raise TypeError("Cannot create an InequalityExpression "\
                      "where one of the sub-expressions is an equality "\
                      "expression:\n    " + buf.getvalue().strip())
        elif rhs_is_relational:
            if rhs.__class__ is _InequalityExpression:
                if len(rhs._args) > 2:
                    raise ValueError("Cannot create an InequalityExpression "\
                          "with more than 3 terms.")
                rhs._args = (lhs,) + rhs._args
                rhs._strict = strict + rhs._strict
                rhs._cloned_from = cloned_from
                return rhs
            else:
                buf = StringIO()
                rhs.to_string(buf)
                raise TypeError("Cannot create an InequalityExpression "\
                      "where one of the sub-expressions is an equality "\
                      "expression:\n    " + buf.getvalue().strip())
        else:
            ans = _InequalityExpression((lhs, rhs), strict, cloned_from)
            return ans

# [functionality] chainedInequality allows us to generate symbolic
# expressions of the type "a < b < c".  This provides a buffer to hold
# the first inequality so the second inequality can access it later.
generate_relational_expression.chainedInequality = None


def generate_intrinsic_function_expression(arg, name, fcn):
    if arg.__class__ in native_types:
        return fcn(arg)

    _process_arg(arg)
    if arg._potentially_variable():
        return _UnaryFunctionExpression(arg, name, fcn)
    return _NPV_UnaryFunctionExpression(arg, name, fcn)


pyomo5_expression_types = set([
        _ExpressionBase,
        _NegationExpression,
        _Constant_NegationExpression,
        _NPV_NegationExpression,
        _ExternalFunctionExpression,
        _Constant_ExternalFunctionExpression,
        _NPV_ExternalFunctionExpression,
        _PowExpression,
        _Constant_PowExpression,
        _NPV_PowExpression,
        _LinearOperatorExpression,
        _InequalityExpression,
        _EqualityExpression,
        _ProductExpression,
        _Constant_ProductExpression,
        _NPV_ProductExpression,
        _MultiProdExpression,
        _ReciprocalExpression,
        _Constant_ReciprocalExpression,
        _NPV_ReciprocalExpression,
        _SumExpression,
        _Constant_SumExpression,
        _NPV_SumExpression,
        _MultiSumExpression,
        _StaticMultiSumExpression,
        _CompressedSumExpression,
        _GetItemExpression,
        Expr_if,
        _UnaryFunctionExpression,
        _Constant_UnaryFunctionExpression,
        _NPV_UnaryFunctionExpression,
        _AbsExpression,
        _Constant_AbsExpression,
        _NPV_AbsExpression
        ])
pyomo5_multisum_types = set([
        _MultiSumExpression,
        _StaticMultiSumExpression,
        _CompressedSumExpression
        ])
pyomo5_product_types = set([
        _ProductExpression,
        _Constant_ProductExpression,
        _NPV_ProductExpression
        ])
pyomo5_reciprocal_types = set([
        _ReciprocalExpression,
        _Constant_ReciprocalExpression,
        _NPV_ReciprocalExpression
        ])

