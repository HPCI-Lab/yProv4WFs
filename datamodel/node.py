from datetime import datetime

#------------------NODE------------------–#
class Node:
    def __init__(self, id: str, name: str):
        self._id = id
        self._name = name
        self._source = None
        self._target = None
        self._start_time = None
        self._end_time = None

    def start(self):
        self._start_time = datetime.now()

    def end(self):
        self._end_time = datetime.now()

    def duration(self):
        if self._start_time and self._end_time:
            return self._end_time - self._start_time
        return None





# import networkx as nx
# import matplotlib.pyplot as plt

# #------------------VISUALIZATION------------------–#

# def visualize_workflow(workflow):
#     G = nx.DiGraph()

#     # Add tasks to the graph
#     for task in workflow._tasks:
#         G.add_node(task._name)

#     # Add edges between tasks
#     for i in range(len(workflow._tasks) - 1):
#         G.add_edge(workflow._tasks[i]._name, workflow._tasks[i + 1]._name)

#     # Draw the graph
#     nx.draw(G, with_labels=True)
#     plt.show()