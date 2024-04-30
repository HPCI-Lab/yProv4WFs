from datamodel.node import Node
from datamodel.data import Data

#------------------TASK------------------–#
class Task(Node):
    def __init__(self, id: str, name: str):
        super().__init__(id, name)
        self._inputs = []
        self._outputs = []

    def get_input(self, data: 'Data'):
        if data.is_input(self):
            self._inputs.append(data)

    def get_output(self, data: 'Data'):
        if data.is_output(self):
            self._outputs.append(data)
    
    def previous_task(self, source: 'Task'):
        self._source = source

    def next_task(self, target: 'Task'):
        self._target = target
