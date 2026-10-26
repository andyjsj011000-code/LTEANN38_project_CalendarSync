from datetime import datetime,timezone
from test_server import server


def test_expired_saved_custom_reminder_does_not_block_offset_changes(server):
    client=server.client()
    task=client.post('/api/tasks',json={'title':'Upcoming task','start_date':'2026-10-23'}).json()
    route='/api/reminders/personal/'+task['id']
    when=datetime.fromtimestamp(server.db.clock()+60,timezone.utc).isoformat()
    assert client.put(route,json={'offsets':[-60],'custom_times':[when]}).status_code==200
    server.now[0]+=120
    assert client.put(route,json={'offsets':[-1440],'custom_times':[when]}).status_code==200
    assert client.get(route).json()=={'offsets':[-1440],'custom_times':[]}
    assert client.put(route,json={'offsets':[-1440],'custom_times':[when]}).status_code==422
