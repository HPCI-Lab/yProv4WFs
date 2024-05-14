from datetime import datetime
from datamodel.enactor import Enactor

#------------------NODE------------------–#
class Node:
    def __init__(self, id: str, name: str):
        self._id = id
        self._name = name
        self._source = None
        self._target = None
        self._start_time = None
        self._end_time = None
        self._enactor = None

    def start(self):
        self._start_time = datetime.now()

    def end(self):
        self._end_time = datetime.now()

    def duration(self):
        if self._start_time and self._end_time:
            return self._end_time - self._start_time
        return None
    
    def set_enactor(self, enactor: 'Enactor'):
        self._enactor = enactor
        enactor._associated_with.append(self)

    def set_source(self, source: 'Node'):
        self._source = source

    def set_target(self, target: 'Node'):
        self._target = target

