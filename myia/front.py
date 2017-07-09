
from myia.ast import \
    Location, Symbol, Literal, \
    LetRec, If, Lambda, Apply, Begin, Tuple
from myia.symbols import get_operator, builtins
from uuid import uuid4 as uuid
import ast
import inspect
import textwrap
import sys


class MyiaSyntaxError(Exception):
    def __init__(self, location, message):
        self.location = location
        self.message = message


_prevhook = sys.excepthook
def exception_handler(exception_type, exception, traceback):
    if (exception_type == MyiaSyntaxError):
        print("{}: {}".format(exception_type.__name__, exception.message), file=sys.stderr)
        print(exception.location.traceback(), file=sys.stderr)
    else:
        _prevhook(exception_type, exception, traceback)
sys.excepthook = exception_handler


class Redirect:
    def __init__(self, key):
        self.key = key


class GenSym:
    def __init__(self, namespace):
        self.varcounts = {}
        self.namespace = namespace

    def name(self, name):
        if name in self.varcounts:
            self.varcounts[name] += 1
            return '{}#{}'.format(name, self.varcounts[name])
        else:
            self.varcounts[name] = 0
            return name

    def sym(self, name, namespace=None):
        return Symbol(self.name(name), namespace=namespace or self.namespace)


class Env:
    def __init__(self, parent=None, namespace=None):
        self.parent = parent
        self.gen = parent.gen if parent else GenSym(namespace or str(uuid()))
        self.bindings = {}

    def get_free(self, name, redirect=True):
        if name in self.bindings:
            free = False
            result = self.bindings[name]
        elif self.parent is None:
            raise NameError("Undeclared variable: {}".format(name))
        else:
            free = True
            result = self.parent[name]
        if redirect and isinstance(result, Redirect):
            return self.get_free(result.key, True)
        else:
            return (free, result)

    def update(self, bindings):
        self.bindings.update(bindings)

    def __getitem__(self, name):
        _, x = self.get_free(name)
        return x

    def __setitem__(self, name, value):
        self.bindings[name] = value


class Locator:
    def __init__(self, url, line_offset):
        self.url = url
        self.line_offset = line_offset

    def __call__(self, node):
        return Location(self.url, node.lineno + self.line_offset - 1, node.col_offset)


class LocVisitor:
    def __init__(self, locator):
        self.locator = locator

    def make_location(self, node):
        return self.locator(node)

    def visit(self, node, **kwargs):
        loc = self.make_location(node)
        cls = node.__class__.__name__
        try:
            method = getattr(self, 'visit_' + cls)
        except AttributeError:
            raise MyiaSyntaxError(loc,
                                  "Unrecognized Python AST node type: {}".format(cls))
        return method(node, loc, **kwargs)


class _Assign:
    def __init__(self, varname, value, location):
        self.varname = varname
        self.value = value
        self.location = location


_c = object()
def group(arr, classify):
    current_c = _c
    results = []
    current = []
    for a in arr:
        c = classify(a)
        if current_c == c:
            current.append(a)
        else:
            if current_c is not _c:
                results.append((current_c, current))
            current_c = c
            current = [a]
    if current_c is not _c:
        results.append((current_c, current))
    return results


class Parser(LocVisitor):

    def __init__(self, parent, global_env=None):
        self.free_variables = {}
        self.local_assignments = set()
        self.returns = False
        
        if isinstance(parent, Locator):
            self.parent = None
            self.env = Env()
            self.globals_accessed = set()
            self.global_env = global_env
            self.return_error = None
            super().__init__(parent)
        else:
            self.parent = parent
            self.env = Env(parent.env)
            self.globals_accessed = parent.globals_accessed
            self.global_env = parent.global_env
            self.return_error = parent.return_error
            super().__init__(parent.locator)

    def gensym(self, name):
        return self.env.gen.sym(name)

    def visit_arguments(self, args):
        return [self.visit(arg) for arg in args.args]

    def visit_body(self, stmts, return_wrapper=False):
        results = []
        for stmt in stmts:
            ret = self.returns
            r = self.visit(stmt)
            if ret:
                raise MyiaSyntaxError(r.location,
                                      "There should be no statements after return.")
            if isinstance(r, Begin):
                results += r.stmts
            else:
                results.append(r)
        groups = group(results, lambda x: isinstance(x, _Assign))
        def helper(groups, result=None):
            (isass, grp), *rest = groups
            if isass:
                bindings = tuple((a.varname, a.value) for a in grp)
                if len(rest) == 0:
                    if result is None:
                        raise MyiaSyntaxError(grp[-1].location, "Missing return statement.")
                    else:
                        return LetRec(bindings, result)
                return LetRec(bindings, helper(rest, result))
            elif len(rest) == 0:
                if len(grp) == 1:
                    return grp[0]
                else:
                    return Begin(grp)
            else:
                return Begin(grp + [helper(rest, result)])

        if return_wrapper:
            return lambda v: helper(groups, v)
        else:
            return helper(groups)

    def visit_Return(self, node, loc):
        if self.return_error:
            raise MyiaSyntaxError(loc, self.return_error)
        self.returns = True
        return self.visit(node.value).at(loc)

    def new_variable(self, base_name):
        sym = self.gensym(base_name)
        self.env.update({base_name: Redirect(sym.label)})
        # The following statement can override the previous, if sym.label == base_name
        # That is fine and intended.
        self.env.update({sym.label: sym})
        return sym

    def make_assign(self, base_name, value, location=None):
        sym = self.new_variable(base_name)
        self.local_assignments.add(base_name)
        return _Assign(sym, value, location)

    def visit_While(self, node, loc):
        fsym = self.global_env.gen.sym('#while')

        p = Parser(self)
        p.return_error = "While loops cannot contain return statements."
        body = p.visit_body(node.body, True)
        in_vars = list(set(p.free_variables.keys()) | set(p.local_assignments))
        out_vars = list(p.local_assignments)

        # We now redo the parsing in order to avoid having free variables
        p = Parser(self)
        in_syms = [p.gensym(v) for v in in_vars]
        p.env.update({v: s for v, s in zip(in_vars, in_syms)})
        test = p.visit(node.test)
        initial_values = [p.env[v] for v in out_vars]
        body = p.visit_body(node.body, True)
        new_body = If(test,
                      body(Apply(fsym, *[p.env[v] for v in in_vars])),
                      Tuple(initial_values))

        self.global_env[fsym.label] = Lambda(fsym.label, in_syms, new_body, location=loc)
        self.globals_accessed.add(fsym.label)

        tmp = self.gensym('#tmp')
        val = Apply(fsym, Tuple(self.env[v] for v in in_vars))
        stmts = [_Assign(tmp, val, None)]
        for i, v in enumerate(out_vars):
            stmt = self.make_assign(v, Apply(builtins.index, tmp, Literal(i)))
            stmts.append(stmt)
        return Begin(stmts)


    def visit_If(self, node, loc):
        p1 = Parser(self)
        body = p1.visit_body(node.body, True)
        p2 = Parser(self)
        orelse = p2.visit_body(node.orelse, True)
        if p1.returns != p2.returns:
            raise MyiaSyntaxError(loc, "Either none or all branches of an if statement must return a value.")
        if p1.local_assignments != p2.local_assignments:
            raise MyiaSyntaxError(loc, "All branches of an if statement must assign to the same set of variables.\nTrue branch sets: {}\nElse branch sets: {}".format(" ".join(sorted(p1.local_assignments)), " ".join(sorted(p2.local_assignments))))

        if p1.returns:
            self.returns = True
            return If(self.visit(node.test),
                      body(None),
                      orelse(None),
                      location=loc)
        else:
            ass = list(p1.local_assignments)
            if len(ass) == 1:
                a, = ass
                val = If(self.visit(node.test),
                         body(p1.env[a]),
                         orelse(p2.env[a]),
                         location=loc)
                return self.make_assign(a, val, None)
            else:
                val = If(self.visit(node.test),
                         body(Tuple(p1.env[v] for v in ass)),
                         orelse(Tuple(p2.env[v] for v in ass)),
                         location=loc)
                tmp = self.gensym('#tmp')
                stmts = [_Assign(tmp, val, None)]
                for i, a in enumerate(ass):
                    stmt = self.make_assign(a, Apply(builtins.index, tmp, Literal(i)))
                    stmts.append(stmt)
                return Begin(stmts)

    def visit_Assign(self, node, loc):
        targ, = node.targets
        if isinstance(targ, ast.Tuple):
            raise MyiaSyntaxError(loc, "Deconstructing assignment is not supported.")
        val = self.visit(node.value)
        return self.make_assign(targ.id, val, loc)

    def visit_FunctionDef(self, node, loc, allow_decorator=False):
        if node.args.vararg:
            raise MyiaSyntaxError(loc, "Varargs are not allowed.")
        if node.args.kwarg:
            raise MyiaSyntaxError(loc, "Varargs are not allowed.")
        if node.args.kwonlyargs:
            raise MyiaSyntaxError(loc, "Keyword-only arguments are not allowed.")
        if node.args.defaults or node.args.kw_defaults:
            raise MyiaSyntaxError(loc, "Default arguments are not allowed.")
        if not allow_decorator and len(node.decorator_list) > 0:
            raise MyiaSyntaxError(loc, "Functions should not have decorators.")
        subp = Parser(self)
        args = [self.gensym(arg.arg) for arg in node.args.args]
        subp.env.update({arg.arg: s for arg, s in zip(node.args.args, args)})
        result = subp.visit_body(node.body)
        if subp.free_variables:
            v, _ = items(subp.free_variables)[0]
            raise MyiaSyntaxError(v.location, "Functions cannot have free variables.")
        if not subp.returns:
            raise MyiaSyntaxError(loc, "Function does not return a value.")
        fn = Lambda(node.name,
                    args,
                    result,
                    location=loc)
        self.global_env[node.name] = fn
        return Symbol(node.name, namespace='global')
        # return fn

    def visit_Lambda(self, node, loc):
        return Lambda("lambda",
                      self.visit_arguments(node.args),
                      self.visit(node.body),
                      location=loc)

    def visit_Expr(self, node, loc, allow_decorator='this is a dummy_parameter'):
        return self.visit(node.value)

    def visit_Name(self, node, loc):
        try:
            free, v = self.env.get_free(node.id)
            if free:
                self.free_variables[node.id] = v
            return v
        except NameError as e:
            # raise MyiaSyntaxError(loc, e.args[0])
            self.globals_accessed.add(node.id)
            return Symbol(node.id, namespace='global')

    def visit_Num(self, node, loc):
        return Literal(node.n)

    def visit_Str(self, node, loc):
        return Literal(node.s)

    def visit_IfExp(self, node, loc):
        return If(self.visit(node.test),
                  self.visit(node.body),
                  self.visit(node.orelse),
                  location=loc)

    def visit_BinOp(self, node, loc):
        op = get_operator(node.op)
        return Apply(op, self.visit(node.left), self.visit(node.right), location=loc)

    def visit_BoolOp(self, node, loc):
        left, right = node.values
        if isinstance(node.op, ast.And):
            return If(self.visit(left), self.visit(right), Literal(False))
        elif isinstance(node.op, ast.Or):
            return If(self.visit(left), Literal(True), self.visit(right))
        else:
            raise MyiaSyntaxError(loc, "Unknown operator: {}".format(node.op))

    def visit_Compare(self, node, loc):
        ops = [get_operator(op) for op in node.ops]
        if len(ops) == 1:
            return Apply(ops[0], self.visit(node.left), self.visit(node.comparators[0]))
        else:
            raise MyiaSyntaxError(loc,
                                  "Comparisons must have a maximum of two operands")
        
    def visit_Subscript(self, node, loc):
        return Apply(builtins.index, self.visit(node.value),
                     self.visit(node.slice.value),
                     location=loc)

    def visit_Call(self, node, loc):
        if (len(node.keywords) > 0):
            raise MyiaSyntaxError(loc, "Keyword arguments are not allowed.")
        return Apply(self.visit(node.func),
                     *[self.visit(arg) for arg in node.args],
                     location=loc)
        

    def visit_ListComp(self, node, loc):
        if len(node.generators) > 1:
            raise MyiaSyntaxError(loc,
                "List comprehensions can only iterate over a single target")
        gen = node.generators[0]
        if len(gen.ifs) > 0:
            test1, *others = reversed(gen.ifs)
            cond = self.visit(test1)
            for test in others:
                cond = If(self.visit(test), cond, Literal(False))
            arg = Apply(builtins.filter,
                        Lambda("filtercmp", [self.visit(gen.target)], cond),
                        self.visit(gen.iter))
        else:
            arg = self.visit(gen.iter)
        return Apply(builtins.map,
                     Lambda("listcmp", [self.visit(gen.target)], self.visit(node.elt)),
                     arg,
                     location=loc)

    def visit_arg(self, node, loc):
        return Symbol(node.arg, location=loc)


def parse_function(fn):
    _, line = inspect.getsourcelines(fn)
    return parse_source(inspect.getfile(fn), line, textwrap.dedent(inspect.getsource(fn)))


def parse_source(url, line, src):
    tree = ast.parse(src).body[0]
    p = Parser(Locator(url, line), Env(namespace='global'))
    r = p.visit(tree, allow_decorator=True)
    # print(p.global_env.bindings)
    # print(p.globals_accessed)
    return r


def myia(fn):
    return parse_function(fn)