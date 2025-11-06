from datetime import datetime, timedelta
from typing import Any

#------------------NODE------------------–#
class Node:
    """
    Represents a Node in a Workflow Management System.
    """
    def __init__(self, id: str, name: str):
        self._id: str = id
        self._name: str = name
        self._start_time: datetime | None = None
        self._end_time: datetime | None = None
        self._agent: Agent | None = None
        self._description: str | None = None
        self._status: Any = None
        self._level: str | None = None

    def start(self) -> datetime:
        self._start_time = datetime.now()
        return self._start_time

    def end(self) -> datetime:
        self._end_time = datetime.now()
        return self._end_time

    def duration(self) -> timedelta | None:
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
        
    def set_id(self, id: str):
        self._id = id

#------------------AGENT------------------–#
class Agent:
    """
    Represents an agent (compliant with W3C-PROV) with an ID and name, and
    maintains relationships with other agents it acts for, is attributed to, or
    is associated with.
    """
    def __init__(self, id: str, name: str):
        self._id: str = id
        self._name: str = name
        self._acted_for: Agent | None = None
        self._attributed_to = []
        self._associated_with: list[Node] = []

    def set_acted_for(self, agent: 'Agent'):
        self._acted_for = agent
    