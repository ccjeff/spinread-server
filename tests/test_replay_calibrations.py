"""Camera windows never share projected frame neighborhoods or erase peers."""
import pytest
from test_replay import replay_client, PATH, BODY
from test_quizzes import quiz_client
from spinread.core.models import ReplayReview, TimelineItem

ENDPOINT='/api/videos/video/replay-review'
A={'id':'camera-a','corners':BODY['corners'],'start_ms':0,'end_ms':1000}
B={'id':'camera-b','corners':[[0,0],[.8,0],[.8,.8],[0,.8]],'start_ms':1000,'end_ms':2000}


def update(c,version,windows,**extra):
    return c.put(ENDPOINT,json={'base_version':version,'timeline_version':1,
        'calibrations':windows,'fixed_camera':True,'reviews':[],**extra})


@pytest.fixture
def cameras(replay_client):
    c,factory,evidence=replay_client
    evidence['frames'] += [[t+1000,x,y] for t,x,y in list(evidence['frames'])]
    with factory.begin() as db:
        db.add(TimelineItem(id='hit3',timeline_id='timeline',parent_id='rally',type='HIT_CANDIDATE',
            start_ms=1600,end_ms=1640))
    return c,factory,evidence


def test_camera_edit_and_removal_preserve_other_confirmations(cameras):
    c,_,_=cameras
    first=update(c,0,[B,A])
    assert first.status_code==200,first.text
    assert [w['id'] for w in first.json()['calibrations']]==['camera-a','camera-b']
    placements=c.get(PATH).json()['placements']
    assert [(p['time_ms'],p['calibration_id']) for p in placements]==[(266,'camera-a'),(1266,'camera-b')]
    assert placements[0]['image_xy']==placements[1]['image_xy']
    assert placements[0]['table_xy']!=placements[1]['table_xy']
    reviews=[{'rally_id':'rally','time_ms':266,'phase':'SERVE','status':'CONFIRMED'},
             {'rally_id':'rally','time_ms':1266,'phase':'RECEIVE','status':'CONFIRMED'}]
    saved=update(c,1,[A,B],reviews=reviews,fixed_camera=None)
    assert saved.status_code==200,saved.text
    revised={**A,'corners':[[0,0],[.9,0],[.9,.9],[0,.9]]}
    conflict=update(c,2,[revised,B],reviews=reviews)
    assert conflict.status_code==422 and conflict.json()['error']['code']=='CALIBRATION_CHANGED'
    changed=update(c,2,[revised,B])
    assert changed.status_code==200,changed.text
    assert changed.json()['reviews']==[reviews[1]]
    placements=c.get(PATH).json()['placements']
    assert [p['status'] for p in placements]==['CANDIDATE','CONFIRMED']
    # Removing the first camera must preserve the second camera's manual review.
    removed=update(c,3,[B],fixed_camera=None)
    assert removed.status_code==200,removed.text
    assert removed.json()['reviews']==[reviews[1]]
    assert [p['time_ms'] for p in c.get(PATH).json()['placements']]==[1266]


def test_legacy_records_are_read_without_rewriting_and_legacy_client_keeps_peers(cameras):
    c,factory,_=cameras
    legacy={k:v for k,v in BODY.items() if k not in ('base_version','timeline_version','fixed_camera')}
    legacy['calibration_end_ms']=1000
    with factory.begin() as db:
        db.add(ReplayReview(video_id='video',reviewer_id='owner',version=1,timeline_version=1,data=legacy))
    review=c.get(PATH).json()['review']
    assert review['calibrations']==[{**A,'id':'legacy'}]
    with factory() as db:
        assert 'calibrations' not in db.get(ReplayReview,'video').data
    assert update(c,1,[{**A,'id':'legacy'},B]).status_code==200
    # Old clients echo only the first camera's old fields; the new list survives.
    old_client=c.put(ENDPOINT,json={**BODY,'base_version':2,'calibration_end_ms':1000})
    assert old_client.status_code==200,old_client.text
    assert old_client.json()['calibrations']==[{**A,'id':'legacy'},B]


def test_no_bounce_window_crosses_camera_cut(replay_client):
    c,factory,evidence=replay_client
    evidence['frames']=[[200,.5,.4],[300,.5,.42],[400,.5,.44],[500,.5,.42],[600,.5,.4]]
    with factory.begin() as db:
        db.add(TimelineItem(id='hit-before-bounce',timeline_id='timeline',parent_id='rally',type='HIT_CANDIDATE',
            start_ms=260,end_ms=261))
    assert update(c,0,[{**A,'end_ms':2000}]).status_code==200
    assert [p['time_ms'] for p in c.get(PATH).json()['placements']]==[400]
    # The second camera contains both bracketing hits, but only four of the
    # five observed samples. Its projection must not borrow the first camera.
    assert update(c,1,[{**A,'end_ms':250},{**B,'start_ms':250}]).status_code==200
    assert c.get(PATH).json()['placements']==[]


def test_calibration_does_not_restore_removed_contacts(cameras):
    c,factory,evidence=cameras
    evidence['contacts']=[{'time_ms':1000,'status':'CANDIDATE'}]
    assert update(c,0,[A,B]).status_code==200
    assert len(c.get(PATH).json()['placements'])==2
    with factory.begin() as db:
        db.get(TimelineItem,'hit2').status='DELETED'
    data=c.get(PATH).json()
    assert 1000 not in [h['time_ms'] for h in data['contacts']]
    assert [p['time_ms'] for p in data['placements']]==[266]


@pytest.mark.parametrize('windows,extra',[
    ([A,{**B,'start_ms':999}],{}),
    ([A,{**B,'id':'camera-a'}],{}),
    ([{**A,'end_ms':20001}],{}),
    ([{**A,'end_ms':0}],{}),
    ([{**A,'corners':[[0,0],[1,1],[1,0],[0,1]]}],{}),
    ([A],{'fixed_camera':None}),
    ([{**A,'id':str(i),'start_ms':i*10,'end_ms':i*10+10} for i in range(101)],{}),
])
def test_invalid_calibration_windows_are_rejected(replay_client,windows,extra):
    c,_,_=replay_client
    assert update(c,0,windows,**extra).status_code==422
    assert c.get(PATH).json()['review']['version']==0


def test_adjacent_camera_windows_and_empty_explicit_list(replay_client):
    c,_,_=replay_client
    saved=update(c,0,[A,B])
    assert saved.status_code==200,saved.text
    removed=update(c,1,[],fixed_camera=None)
    assert removed.status_code==200
    assert removed.json()['calibrations']==[] and removed.json()['corners'] is None
    assert c.get(PATH).json()['placements']==[]
