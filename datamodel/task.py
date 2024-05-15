from datamodel.node import Node
from datamodel.data import Data

#------------------TASK------------------–#
class Task(Node):
    def __init__(self, id: str, name: str):
        super().__init__(id, name)
        self._inputs = []
        self._outputs = []

    def add_input(self, data: Data):
        data.set_consumer(self)
        if data.is_input:
            self._inputs.append(data)

    def add_output(self, data: Data):
        data.set_producer(self)
        if data.is_output:
            self._outputs.append(data)
    
    # def to_dict(self):
    #     return {
    #         'id': self._id,
    #         'start_time': self._start_time,
    #         'end_time': self._end_time,
    #         'name': self._name,
    #         'inputs': self._inputs,
    #         'outputs': self._outputs,
    #         'source': self._source,
    #         'target': self._target,
    #         'enactor': self._enactor
    #     }
