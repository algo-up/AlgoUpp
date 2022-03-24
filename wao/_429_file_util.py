import os

from wao.brain_config import BrainConfig

minimum_string_length = 80


def parse_romeo_message_to_429_title(text):
    print("parse_romeo_message_to_429_title:...")
    if len(text) > minimum_string_length:
        execution_id = text.split('\n')[3].split(':')[1].replace(" ", "").replace("*", "")
        action_1 = text.split('\n')[0].split(':')[1].replace(" ", "").replace("*", "")
        action_2 = text.split('\n')[0].split(':')[2].replace(" ", "").replace("*", "")
        file_name = BrainConfig._429_DIRECTORY + execution_id + '_' + action_1 + '_' + action_2
        return file_name
    else:
        return None


def delete_429_file(text):
    print("delete_429_file:...")
    file_name = parse_romeo_message_to_429_title(text)
    if file_name is not None:
        if os.path.isfile(file_name):
            os.remove(file_name)


def write_to_429_file(text):
    print("write_to_429_file:...")
    file_name = parse_romeo_message_to_429_title(text)
    if file_name is not None:
        with open(file_name, 'w') as file:
            file.write(text)
        file.close()


def is_response_from_romeo_200(text, is_from_romeo) -> bool:
    if text == "<Response [200]>" and is_from_romeo:
        return True
    else:
        return False
