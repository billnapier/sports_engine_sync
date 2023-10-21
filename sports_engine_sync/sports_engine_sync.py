"""Sync ical to sportsengine events."""

import json
import re
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
import os.path
from urllib.parse import urljoin

import html5lib
import requests
from absl import logging
from configobj import ConfigObj
import icalendar

# a lot borrowed from https://github.com/google/github_nonpublic_api.  Which means that maybe some of that should live in it's own library?


def _get_form(session, url: str):
    logging.info('Fetching URL %s', url)
    response = session.get(url)
    response.raise_for_status()
    return response


def _submit_form(session, url: str, text: str, data_callback=None, form_matcher=lambda form: True):
    doc = html5lib.parse(text, namespaceHTMLElements=False)
    forms = doc.findall('.//form')

    submit_form = None
    for form in forms:
        if form_matcher(form):
            submit_form = form
            break
    if submit_form is None:
        raise ValueError('Unable to find form')

    action_url = submit_form.attrib['action']
    # Look at all the inputs under the given form.
    inputs = submit_form.findall('.//input')

    data = dict()
    for form_input in inputs:
        value = form_input.attrib.get('value')
        if value and 'name' in form_input.attrib:
            data[form_input.attrib['name']] = value

    # Have the caller provide additional data
    if data_callback:
        data_callback(data)

    logging.debug('Form data: %s', str(data))

    submit_url = urljoin(url, action_url)
    logging.info('Posting form to URL %s', submit_url)

    response = session.post(submit_url, data=data)
    response.raise_for_status()
    return response


def _get_and_submit_form(session, url: str, data_callback=None, form_matcher=lambda form: True):
    response = _get_form(session=session, url=url)
    return _submit_form(session=session, url=url, text=response.text, data_callback=data_callback, form_matcher=form_matcher)


def _get_url_with_session(session, url: str):
    logging.info('Fetching URL %s', url)
    response = session.get(url)
    response.raise_for_status()
    return response


def create_login_session(username: str, password: str,
                         session: requests.Session = None) -> requests.Session:
    """Create a requests.Session object with logged in GitHub cookies for the user."""
    session = session or requests.Session()

    def _username_callback(data):
        data.update({"user[login]": username})
    response = _get_and_submit_form(
        session=session, url='https://user.sportngin.com/users/sign_in', data_callback=_username_callback)

    def _login_callback(data):
        data.update({"user[password]": password})
    _submit_form(
        session=session, url=response.url, text=response.text, data_callback=_login_callback)

    return session


# 2023-10-31T23:59:59-07:00
_LIST_EVENTS_URL = 'https://api.sportngin.com/v3/calendar/team/%s?end_date=%s&order_by=start_date&page=1&per_page=200&show_event_attendees=1&start_date=%s'
_CREATE_OPPONENT_URL = 'https://api.sportngin.com/v3/teams/%s/opponents'
_ADD_EVENT_URL = 'https://api.sportngin.com/v3/calendar/team/%s/event'
_LIST_OPPONENT_URL = 'https://api.sportngin.com/v3/teams/%s/opponents?page=1&per_page=100'


def _datetime_to_string(d: datetime):
    return d.isoformat(timespec='seconds')


def list_opponents(session, team_id: str):
    url = _LIST_OPPONENT_URL % (team_id)
    logging.info("list_events: %s", url)
    response = session.get(url=url)
    response.raise_for_status()

    return response.json()


def list_events(session, team_id: str, end_date: datetime, start_date: datetime):
    url = _LIST_EVENTS_URL % (team_id, _datetime_to_string(
        end_date), _datetime_to_string(start_date))
    logging.info("list_events: %s", url)
    response = session.get(url=url)
    response.raise_for_status()

    return response.json()


def create_new_opponent(session, team_id: str, opponent_name: str):
    url = _CREATE_OPPONENT_URL % (team_id)
    logging.info("create_new_opponent: %s", url)
    response = session.post(url=url, data=dict(name=opponent_name))
    response.raise_for_status()

    return response.json()


def create_event_dict(team_id: str, start_time: datetime, end_time: datetime, title: str):
    return dict(event_type='event',
                status='scheduled',
                local_timezone='America/Los_Angeles',
                with_notification=False,
                principals=[dict(id=team_id, extended_attributes=dict())],
                venue_id=None,
                subvenue_id=None,
                title=title,
                type="event",
                tbd_time=False,
                start_date_time=_datetime_to_string(start_time),
                end_date_time=_datetime_to_string(end_time))


def create_game_dict(team_id: str, team_name: str, opponent_id: str, opponent_name: str, is_home_game: bool, start_time: datetime):
    end_time = start_time + timedelta(hours=1)
    return dict(event_type='game',
                venue_id=None,
                subvenue_id=None,
                local_timezone='America/Los_Angeles',
                game_details={"team_1": dict(id=team_id, is_home_team=is_home_game, name=team_name),
                              "team_2": dict(id=opponent_id, name=opponent_name)
                              },
                with_notification=False,
                principals=[dict(id=team_id, extended_attributes=dict())],
                duration_hours="1",
                duration_minutes="0",
                type="game",
                tbd_time=False,
                start_date_time=_datetime_to_string(start_time),
                end_date_time=_datetime_to_string(end_time))


def add_event(session, team_id: str, data):
    url = _ADD_EVENT_URL % (team_id)
    logging.info("add_event: %s", url)
    logging.info("add_event data: %s", data)

    response = session.post(url=url, data=json.dumps(data), headers={   
        "Content-Type": "application/json;charset=UTF-8",
    })
    response.raise_for_status()

    return response.json()


def _find_first_opponent(session, team_id: str, opponent_name: str):
    for o in list_opponents(session=session, team_id=team_id).get('result'):
        if o.get('name') == opponent_name:
            return o

    return None

#import http.client as http_client
#http_client.HTTPConnection.debuglevel = 1

_ICAL_DESC_RE = re.compile(r'^\w+ \(\w\) (\w+ 12U) @ (\w+ 12U)')

def main():
    config = ConfigObj(os.path.expanduser('~/sports_engine.ini'), _inspec=True)

    s = requests.Session()
    cal = icalendar.Calendar.from_ical(s.get(config.get('calendar')).text)

    session = create_login_session(username=config['username'], password=config['password'])

    for event in cal.walk('VEVENT'):
        start = event.get('DTSTART')
        end = event.get('DTEND')
        summary = event.get('SUMMARY')


        desc = event.get('DESCRIPTION')
        if desc.startswith('Practice'):
            data = create_event_dict(team_id=config['teamid'], start_time=start.dt, end_time=end.dt, title='(Practice) ' + summary)
        else:
            data = create_event_dict(team_id=config['teamid'], start_time=start.dt, end_time=end.dt, title='(Game) ' + summary)
        add_event(session=session, team_id=config['teamid'], data=data)
            

if __name__ == "__main__":
    logging.set_verbosity(1)
    main()