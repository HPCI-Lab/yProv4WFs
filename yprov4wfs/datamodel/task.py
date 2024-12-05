from yprov4wfs.datamodel.node import Node
from yprov4wfs.datamodel.data import Data

#------------------TASK------------------–#
"""
Task class represents a unit of work in a workflow, inheriting from Node.
Attributes:
    _inputs (list): List of Data objects that are inputs to the task.
    _outputs (list): List of Data objects that are outputs from the task.
    _prev (list): List of preceding Task objects.
    _next (list): List of succeeding Task objects.
Methods:
    add_input(data: Data):
        Adds a Data object to the task's inputs if it is marked as an input.
    add_output(data: Data):
        Adds a Data object to the task's outputs if it is marked as an output.
    set_prev(prev: 'Task'):
        Sets a preceding Task object.
    set_next(next: 'Task'):
        Sets a succeeding Task object.
"""

class Task(Node):
    def __init__(self, id: str, name: str):
        super().__init__(id, name)
        self._inputs = []
        self._outputs = []
        self._prev = []
        self._next = []
        self._manual_submit = None
        self._run_platform = None
        self._delay = None
        self._timeout = None

    def add_input(self, data: Data):
        data.set_consumer(self._name)
        if data.is_input:
            self._inputs.append(data)

    def add_output(self, data: Data):
        data.set_producer(self._name)
        if data.is_output:
            self._outputs.append(data)
            
    def set_prev(self, prev: 'Task'):
        self._prev.append(prev)

    def set_next(self, next: 'Task'):
        self._next.append(next)   
