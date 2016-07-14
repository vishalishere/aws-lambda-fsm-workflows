# system imports
import uuid
import json
import time
import logging

# library imports

# application imports
from aws_lambda_fsm.aws import send_next_event_for_dispatch
from aws_lambda_fsm.aws import send_next_events_for_dispatch
from aws_lambda_fsm.constants import SYSTEM_CONTEXT
from aws_lambda_fsm.constants import STATE
from aws_lambda_fsm.constants import PAYLOAD

logger = logging.getLogger(__name__)


def start_state_machine(machine_name,
                        initial_context,
                        correlation_id=None,
                        current_state=STATE.PSEUDO_INIT,
                        current_event=STATE.PSEUDO_INIT):
    """
    Insert a AWS Kinesis message that will kick off a state machine.

    :param machine_name: a str name for the machine to start.
    :param initial_context: a dict of initial data for the state machine.
    :param correlation_id: the guid for the fsm, or None if the system should
      define it automatically.
    :param current_state: the state to start the machine in.
    :param current_event: the event to start the machine with.

    """
    correlation_id = correlation_id or uuid.uuid4().hex
    system_context = {
        SYSTEM_CONTEXT.STARTED_AT: int(time.time()),
        SYSTEM_CONTEXT.MACHINE_NAME: machine_name,
        SYSTEM_CONTEXT.CURRENT_STATE: current_state,
        SYSTEM_CONTEXT.CURRENT_EVENT: current_event,
        SYSTEM_CONTEXT.STEPS: 0,
        SYSTEM_CONTEXT.RETRIES: 0,
        SYSTEM_CONTEXT.CORRELATION_ID: correlation_id,
    }
    payload = {
        PAYLOAD.VERSION: PAYLOAD.DEFAULT_VERSION,
        PAYLOAD.SYSTEM_CONTEXT: system_context,
        PAYLOAD.USER_CONTEXT: initial_context
    }
    send_next_event_for_dispatch(None,
                                 json.dumps(payload, sort_keys=True),
                                 correlation_id)


def start_state_machines(machine_name,
                         user_contexts,
                         correlation_ids=None,
                         current_state=STATE.PSEUDO_INIT,
                         current_event=STATE.PSEUDO_INIT):
    """
    Insert a bulk AWS Kinesis message that will kick off several state machines.

    :param machine_name: a str name for the machine to start.
    :param user_contexts: a list of dict of initial data for the state machines.
    :param correlation_ids: a list of guids for the fsms, or list of Nones
      if the system should define then automatically.
    :param current_state: the state to start the machines in.
    :param current_event: the event to start the machines with.
    """
    all_data = []
    correlation_ids = correlation_ids or [uuid.uuid4().hex for i in range(len(user_contexts))]
    for i, user_context in enumerate(user_contexts):
        correlation_id = correlation_ids[i]
        started_at = int(time.time())
        system_context = {
            SYSTEM_CONTEXT.STARTED_AT: started_at,
            SYSTEM_CONTEXT.MACHINE_NAME: machine_name,
            SYSTEM_CONTEXT.CURRENT_STATE: current_state,
            SYSTEM_CONTEXT.CURRENT_EVENT: current_event,
            SYSTEM_CONTEXT.STEPS: 0,
            SYSTEM_CONTEXT.RETRIES: 0,
            SYSTEM_CONTEXT.CORRELATION_ID: correlation_id,
        }
        payload = {
            PAYLOAD.VERSION: PAYLOAD.DEFAULT_VERSION,
            PAYLOAD.SYSTEM_CONTEXT: system_context,
            PAYLOAD.USER_CONTEXT: user_context
        }
        all_data.append(json.dumps(payload, sort_keys=True))
    send_next_events_for_dispatch(None,
                                  all_data,
                                  correlation_ids)