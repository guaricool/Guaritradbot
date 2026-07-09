import yaml
import os

class WorkflowEngine:
    """
    Parses and executes YAML-based workflows for multi-agent orchestration.
    """
    def __init__(self, agents_registry):
        self.agents = agents_registry

    def load_workflow(self, filepath: str):
        with open(filepath, 'r') as file:
            return yaml.safe_load(file)

    def run(self, workflow_data: dict):
        print(f"Starting workflow: {workflow_data.get('name')}")
        steps = workflow_data.get('steps', [])
        
        state = {}
        for step in steps:
            step_id = step['id']
            agent_name = step['agent']
            action_name = step['action']
            inputs = step.get('inputs', {})
            
            print(f"[{step_id}] Delegating to {agent_name} -> {action_name}")
            
            if agent_name not in self.agents:
                raise ValueError(f"Agent {agent_name} not found in registry")
            
            agent = self.agents[agent_name]
            action_method = getattr(agent, action_name, None)
            
            if not action_method:
                raise ValueError(f"Action {action_name} not found on {agent_name}")
            
            # Pass inputs and current state to the agent
            result = action_method(inputs=inputs, state=state)
            state[step_id] = result
        
        print(f"Workflow '{workflow_data.get('name')}' completed.")
        return state
