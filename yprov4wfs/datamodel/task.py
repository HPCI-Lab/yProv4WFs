from yprov4wfs.datamodel.node import Node
from yprov4wfs.datamodel.data import Data

#------------------TASK------------------–#
class Task(Node):
    def __init__(self, id: str, name: str):
        super().__init__(id, name)
        self._inputs = []
        self._outputs = []
        self._prev = []
        self._next = []

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
