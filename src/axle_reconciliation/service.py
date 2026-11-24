from __future__ import annotations
from datetime import datetime, timezone


def _time(value: object) -> datetime:
    if not isinstance(value,str): raise ValueError("time must be an ISO string")
    try: result=datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError as exc: raise ValueError("invalid timestamp") from exc
    if result.tzinfo is None: raise ValueError("timestamp needs an offset")
    return result.astimezone(timezone.utc)


def process(data: dict) -> dict:
    sections=data.get("sections") if isinstance(data,dict) else None
    events=data.get("events") if isinstance(data,dict) else None
    if not isinstance(sections,list) or not 1<=len(sections)<=1000: raise ValueError("sections must contain 1..1000 items")
    if not isinstance(events,list) or not 1<=len(events)<=100000: raise ValueError("events must contain 1..100000 items")
    section_map={}; checkpoint_owner={}
    for section in sections:
        sid=section.get("id") if isinstance(section,dict) else None
        entry=section.get("entry") if isinstance(section,dict) else None
        exit_=section.get("exit") if isinstance(section,dict) else None
        if not all(isinstance(x,str) and x for x in (sid,entry,exit_)) or sid in section_map or entry==exit_: raise ValueError("invalid or duplicate section")
        for cp in (entry,exit_):
            if cp in checkpoint_owner: raise ValueError("checkpoints may belong to one section only")
            checkpoint_owner[cp]=sid
        section_map[sid]=(entry,exit_)
    ids=set(); parsed=[]
    for event in events:
        eid=event.get("id") if isinstance(event,dict) else None
        train=event.get("train_id") if isinstance(event,dict) else None
        checkpoint=event.get("checkpoint") if isinstance(event,dict) else None
        count=event.get("axles") if isinstance(event,dict) else None
        if not isinstance(eid,str) or not eid or eid in ids: raise ValueError("event ids must be unique")
        if not isinstance(train,str) or not train or checkpoint not in checkpoint_owner: raise ValueError("invalid train or checkpoint")
        if isinstance(count,bool) or not isinstance(count,int) or not 1<=count<=1000: raise ValueError("axles must be an integer 1..1000")
        ids.add(eid); parsed.append((_time(event.get("time")),eid,train,checkpoint,count))
    parsed.sort()
    state={sid:{} for sid in section_map}; movements=[]; anomalies=[]
    for time,eid,train,checkpoint,count in parsed:
        sid=checkpoint_owner[checkpoint]; entry,exit_=section_map[sid]
        active=state[sid]
        if checkpoint==entry:
            if train in active:
                anomalies.append({"event_id":eid,"section_id":sid,"train_id":train,"type":"duplicate_entry"})
            else:
                active[train]=(eid,time,count)
        else:
            opened=active.pop(train,None)
            if opened is None:
                anomalies.append({"event_id":eid,"section_id":sid,"train_id":train,"type":"exit_without_entry"})
            else:
                entry_id,entry_time,entry_count=opened
                movements.append({"section_id":sid,"train_id":train,"entry_event_id":entry_id,"exit_event_id":eid,"entry_axles":entry_count,"exit_axles":count,"duration_seconds":int((time-entry_time).total_seconds()),"status":"balanced" if entry_count==count else "axle_mismatch"})
    occupied=[]
    for sid,active in state.items():
        for train,(eid,_,count) in active.items(): occupied.append({"section_id":sid,"train_id":train,"entry_event_id":eid,"entry_axles":count})
    movements.sort(key=lambda x:(x["section_id"],x["entry_event_id"])); anomalies.sort(key=lambda x:x["event_id"]); occupied.sort(key=lambda x:(x["section_id"],x["train_id"]))
    return {"movements":movements,"anomalies":anomalies,"occupied":occupied}

