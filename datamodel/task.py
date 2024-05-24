from datamodel.node import Node
from datamodel.data import Data

#------------------TASK------------------–#
class Task(Node):
    def __init__(self, id: str, name: str):
        super().__init__(id, name)
        self._inputs = []
        self._outputs = []
        self._source = None
        self._target = None

    def add_input(self, data: Data):
        data.set_consumer(self._name)
        if data.is_input:
            self._inputs.append(data)

    def add_output(self, data: Data):
        data.set_producer(self._name)
        if data.is_output:
            self._outputs.append(data)
            
    def set_source(self, source: 'Task'):
        self._source = source

    def set_target(self, target: 'Task'):
        self._target = target   
