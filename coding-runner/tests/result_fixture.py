import json


SESSION_ID = '0199a213-81c0-7800-8aa1-bbab2a035a53'


def event_stream(message, session_id=SESSION_ID, completed=True):
    events = [{'type': 'thread.started', 'thread_id': session_id},
              {'type': 'item.completed', 'item': {'id': 'answer', 'type': 'agent_message', 'text': message}}]
    if completed:
        events.append({'type': 'turn.completed', 'usage': {}})
    return ''.join(json.dumps(event) + '\n' for event in events)


def model_result(status='completed', **fields):
    value = {'status': status, 'summary': 'Implemented and validated',
             'tests': ['Regression tests passed'],
             'test_results': [{'command': 'python3 -m unittest', 'exit_code': 0, 'summary': 'All passed'}],
             'next_steps': [], 'blocker': None}
    value.update(fields)
    return value
