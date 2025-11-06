from __future__ import annotations
from typing import Any
from yprov4wfs.datamodel.core import Node
from yprov4wfs.datamodel.data import Data

#------------------TASK------------------–#
class Task(Node):
    """
    Task class represents a unit of work in a workflow, inheriting from Node.
    Attributes:
        `_inputs: list[Data]`: List of Data objects that are inputs to the task.
        `_outputs: list[Data]`: List of Data objects that are outputs from the task.
        `_prev: list[Task]`: List of preceding Task objects.
        `_next: list[Task]`: List of succeeding Task objects.
    Methods:
        `add_input(data: Data)`:
            Adds a Data object to the task's inputs if it is marked as an input.
        `add_output(data: Data)`:
            Adds a Data object to the task's outputs if it is marked as an output.
        `set_prev(prev: Task)`:
            Sets a preceding Task object.
        `set_next(next: Task)`:
            Sets a succeeding Task object.
    """

    def __init__(self, id: str, name: str):
        super().__init__(id, name)
        self._secondary_inputs: list[Data] = []
        self._inputs: list[Data] = []
        self._outputs: list[Data] = []
        self._prev: list[Task] = []
        self._next: list[Task] = []
        self._manual_submit: bool | None = None
        self._run_platform: str | None = None
        self._delay = None
        self._timeout = None
        self._info: Any = None

    def add_secondary_input(self, data: Data):
        self._secondary_inputs.append(data)

    def add_input(self, data: Data):
        data.add_consumer(self)
        if data.is_input:
            self._inputs.append(data)

    def add_output(self, data: Data):
        data.set_producer(self)
        if data.is_output:
            self._outputs.append(data)
            
    def add_prev(self, prev: Task):
        self._prev.append(prev)

    def add_next(self, next: Task):
        self._next.append(next)   
