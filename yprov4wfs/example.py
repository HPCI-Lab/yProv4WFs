from datamodel.workflow import Workflow
from datamodel.task import Task
from datamodel.data import Data, FileType
from datamodel.agent import Agent

def test_workflow_to_prov():
    # Create some Enactor objects
    agent1 = Agent('agent1','Agent 1')
    agent2 = Agent('agent2','Agent 2')

    # Create some Data objects
    data1 = Data('data1', 'Data 1')
    data2 = Data('data2', 'Data 2')
    data3 = Data('data3', 'Data 3')
    data1.set_agent(agent1)
    data2.set_agent(agent2)
    data3.set_agent(agent1)

    # Create some Task objects
    task1 = Task('task1', 'Task 1')
    task2 = Task('task2', 'Task 2')
    task1.add_input(data1)
    task1.add_output(data2)
    task2.add_input(data2)
    task2.add_output(data3)
    task1.set_agent(agent1)
    task2.set_agent(agent2)
    task1.set_next(task2)
    task2.set_prev(task1)


    # Create a Workflow object and add the tasks to it
    workflow = Workflow('wfs1', 'Workflow 1')
    workflow._start_time = workflow.start()
    workflow.add_task(task1)
    workflow.add_task(task2)
    workflow._end_time = workflow.end()

    workflow.prov_to_json()


test_workflow_to_prov()