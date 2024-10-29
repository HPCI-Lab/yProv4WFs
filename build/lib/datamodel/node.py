from datetime import datetime
from datamodel.agent import Agent

#------------------NODE------------------–#
class Node:
    def __init__(self, id: str, name: str):
        self._id = id
        self._name = name
        self._start_time = None
        self._end_time = None
        self._agent = None
        self._description = None
        self._status = None
        self._level = None

    def start(self):
        self._start_time = datetime.now()
        return self._start_time

    def end(self):
        self._end_time = datetime.now()
        return self._end_time

    def duration(self):
        if self._start_time and self._end_time:
            return self._end_time - self._start_time
        return None
    
    def set_agent(self, agent: 'Agent'):
        self._agent = agent
        agent._associated_with.append(self)
        
    def set_level(self, level: str):
        self._level = level

    def add_description(self, description: str):
        self._description = description
