"""Intermediate representation definition.

Myia's main intermediate representation (IR) is a graph-based version of ANF.
Each function definition (lambda) is defined as a graph, consisting of a series
of function applications.

A function can be applied to a node from another funtion's graph; this
implicitly creates a nested function. Functions are first-class objects, so
returning a nested function creates a closure.

"""
import types
from ast import AST
from typing import (List, Set, Tuple, Any, Sequence, MutableSequence,
                    overload, Iterable)

from myia.ir import Node
from myia.utils import Named

PARAMETER = Named('PARAMETER')
APPLY = Named('APPLY')


class Graph:
    """A function graph.

    Attributes:
        parameters: The parameters of this function as a list of `Parameter`
            nodes. Parameter nodes that are unreachable by walking from the
            output node correspond to unused parameters.
        return_: The `Apply` node that calls the `Return` primitive. The input
            to this node will be returned by this function. A graph initially
            has no output node (because it won't be known e.g. until the
            function has completed parsing), but it must be set afterwards for
            the graph instance to be valid.

    """

    def __init__(self) -> None:
        """Construct a graph."""
        self.parameters: List[Parameter] = []
        self.return_: Apply = None
        self.debug = GraphDebug()

    def __str__(self) -> str:
        """Return a string representation of this graph.

        The debug names of the graph and/or nodes will be used to output
        something like `f(x, y) → z` if possible.

        """
        name = self.debug.name if hasattr(self.debug, 'name') else 'func'
        args = ', '.join(str(parameter) for parameter in self.parameters)
        if self.return_ and len(self.return_.inputs) > 1:
            rval = self.return_.inputs[1]
            if hasattr(rval.debug, 'name'):
                return_ = rval.debug.name
            else:
                return_ = str(rval)
        else:
            return_ = '?'
        return f"{name}({args}) → {return_}"


class GraphDebug(types.SimpleNamespace):
    """Debug information for a graph.

    Any information that is used for debugging e.g. plotting, printing, etc.
    can be stored in this class.

    Attributes:
        ast: The AST node that created this graph.
        name: The function name.

    """

    ast: AST
    name: str


class ANFNode(Node):
    """A node in the graph-based ANF IR.

    There are three types of nodes: Function applications; parameters; and
    constants such as numbers and functions.

    Attributes:
        inputs: If the node is a function application, the first node input is
            the function to apply, followed by the arguments. These are use-def
            edges. For other nodes, this attribute is empty.
        value: The value of this node if it is a constant. Parameters and
            function applications have the special values `PARAMETER` and
            `APPLY`.
        graph: The function definition graph that this node belongs to for
            values and parameters (constants don't belong to any function).
        uses: A set of tuples with the nodes that use this node alongside with
            the index. These def-use edges are the reverse of the `inputs`
            attribute, creating a doubly linked graph structure. Note that this
            container is updated automatically; do not manipulate it manually.
        debug: An object with debug information about this node e.g. a
            human-readable name and the Python source code.

    """

    def __init__(self, inputs: Iterable['ANFNode'], value: Any,
                 graph: Graph) -> None:
        """Construct a node."""
        self._inputs = Inputs(self, inputs)
        self.value = value
        self.graph = graph
        self.uses: Set[Tuple[ANFNode, int]] = set()
        self.debug = ANFNodeDebug()

    @property
    def inputs(self) -> 'Inputs':
        """Return the list of inputs."""
        return self._inputs

    @inputs.setter
    def inputs(self, value: Iterable['ANFNode']) -> None:
        """Set the list of inputs."""
        self._inputs.clear()  # type: ignore
        self._inputs = Inputs(self, value)

    @property
    def incoming(self) -> Iterable['ANFNode']:
        """Return incoming nodes in order."""
        return iter(self.inputs)

    @property
    def outgoing(self) -> Iterable['ANFNode']:
        """Return uses of this node in random order."""
        return (node for node, index in self.uses)

    def __copy__(self) -> 'ANFNode':
        """Copy this node.

        This method is used by the `copy` module. It ensures that copied nodes
        will have correct `uses` information. Debug information is not copied.

        """
        cls = self.__class__
        obj = cls.__new__(cls)  # type: ignore
        ANFNode.__init__(obj, self.inputs, self.value, self.graph)
        return obj


class ANFNodeDebug(types.SimpleNamespace):
    """Debug information for a node.

    Any information that is used for debugging e.g. plotting, printing, etc.
    can be stored in this class.

    Attributes:
        ast: The AST node that generated this node.
        name: The name of this variable.

    """

    ast: AST
    name: str


class Inputs(MutableSequence[ANFNode]):
    """Container data structure for node inputs.

    This mutable sequence data structure can be used to keep track of a node's
    inputs. Any insertion or deletion of edges will be reflected in the inputs'
    `uses` attributes.

    """

    def __init__(self, node: ANFNode,
                 initlist: Iterable[ANFNode] = None) -> None:
        """Construct the inputs container for a node.

        Args:
            node: The node of which the inputs are stored.
            initlist: A sequence of nodes to initialize the container with.

        """
        self.node = node
        self.data: List[ANFNode] = []
        if initlist is not None:
            self.extend(initlist)

    @overload
    def __getitem__(self, index: int) -> ANFNode:
        pass

    @overload  # noqa: F811
    def __getitem__(self, index: slice) -> Sequence[ANFNode]:
        pass

    def __getitem__(self, index):  # noqa: F811
        """Get an input by its index."""
        return self.data[index]

    @overload
    def __setitem__(self, index: int, value: ANFNode) -> None:
        pass

    @overload  # noqa: F811
    def __setitem__(self, index: slice, value: Iterable[ANFNode]) -> None:
        pass

    def __setitem__(self, index, value):  # noqa: F811
        """Replace an input with another."""
        if isinstance(index, slice):
            raise ValueError("slice assignment not supported")
        if index < 0:
            index += len(self)
        old_value = self.data[index]
        old_value.uses.remove((self.node, index))
        value.uses.add((self.node, index))
        self.data[index] = value

    @overload
    def __delitem__(self, index: int) -> None:
        pass

    @overload  # noqa: F811
    def __delitem__(self, index: slice) -> None:
        pass

    def __delitem__(self, index):  # noqa: F811
        """Delete an input."""
        if isinstance(index, slice):
            raise ValueError("slice deletion not supported")
        if index < 0:
            index += len(self)
        value = self.data[index]
        value.uses.remove((self.node, index))
        for i, next_value in enumerate(self.data[index + 1:]):
            next_value.uses.remove((self.node, i + index + 1))
            next_value.uses.add((self.node, i + index))
        del self.data[index]

    def __len__(self) -> int:
        """Get the number of inputs."""
        return len(self.data)

    def insert(self, index: int, value: ANFNode) -> None:
        """Insert an input at a given location."""
        if index < 0:
            index += len(self)
        for i, next_value in enumerate(reversed(self.data[index:])):
            next_value.uses.remove((self.node, len(self) - i - 1))
            next_value.uses.add((self.node, len(self) - i))
        value.uses.add((self.node, index))
        self.data.insert(index, value)

    def __repr__(self) -> str:
        """Return a string representation of the inputs."""
        return f"Inputs({self.data})"

    def __eq__(self, other) -> bool:
        """Test whether a list of inputs is equal to another list.

        Note:
            The goal of `Inputs` is to behave exactly like a list, but track
            insertions and deletions to keep the bidirectional graph structure
            up to date. As such, which node an `Inputs` object is attached to
            is irrelevant to the equality test. Instead, a simple element by
            element test is performed.

        """
        return all(x == y for x, y in zip(self, other))


class Apply(ANFNode):
    """A function application.

    This node represents the application of a function to a set of arguments.

    """

    def __init__(self, inputs: List[ANFNode], graph: 'Graph') -> None:
        """Construct an application."""
        super().__init__(inputs, APPLY, graph)

    def __str__(self) -> str:
        """Return a string representation of this node.

        The debugging `name` attribute will be used to output strings of the
        form `z = f(x, y)` or `f(x, y)` if possible.

        """
        inputs = [input_.debug.name if hasattr(input_.debug, 'name')
                  else str(input_) for input_ in self.inputs]
        call = f"{inputs[0]}({', '.join(inputs[1:])})"
        if hasattr(self.debug, 'name'):
            return f"{self.debug.name} = {call}"
        return call


class Parameter(ANFNode):
    """A parameter to a function.

    Parameters are leaf nodes, since they are not the result of a function
    application, and they have no value. They are entirely defined by the graph
    they belong to.

    """

    def __init__(self, graph: Graph) -> None:
        """Construct the parameter."""
        super().__init__([], PARAMETER, graph)

    def __str__(self) -> str:
        """Return a string representation of this node.

        The debugging `name` attribute will be used to output strings of the
        form `x` if possible. Otherwise it will default to `arg0` where `0` is
        the argument index.

        """
        if hasattr(self.debug, 'name'):
            return self.debug.name
        if self in self.graph.parameters:
            return f"arg{self.graph.parameters.index(self)}"
        return "arg"


class Constant(ANFNode):
    """A constant node.

    A constant is a node which is not the result of a function application. In
    the graph it is a leaf node. It has no inputs, and instead is defined
    entirely by its value. Unlike parameters and values, constants do not
    belong to any particular function graph.

    Two "special" constants are those whose value is a `Primitive`
    (representing primitive operations) or whose value is a `Graph` instance
    (representing functions).

    """

    def __init__(self, value: Any) -> None:
        """Construct a literal."""
        super().__init__([], value, None)

    def __str__(self) -> str:
        """Return a string representation of this node.

        The string representation of the value will be used.

        """
        return str(self.value)
