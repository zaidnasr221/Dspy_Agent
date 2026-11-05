"""Cumulative scheduling reservations; CLI-reported costs are estimates."""
import math
import uuid
from .locking import exclusive_lock
from .storage import dump,read


class BudgetStopped(RuntimeError):pass


class Budget:
    def __init__(self,run,cap,reserve):
        if not all(math.isfinite(x) and x>0 for x in [cap,reserve]):raise ValueError('Explicit positive budget and per-call reservation required')
        self.path=run/'cost_ledger.json';self.lock=run/'.budget.lock';self.cap=cap;self.reserve_amount=reserve

    def state(self):
        rows=read(self.path) if self.path.exists() else []
        settled=sum(r.get('reported_cost_usd',0) for r in rows)
        held=sum(r['reserved_usd'] for r in rows if r['status']!='settled')
        return rows,settled,held

    def reserve(self,request_hash,preflight):
        with exclusive_lock(self.lock):
            rows,spent,held=self.state()
            if spent+held+self.reserve_amount>self.cap:raise BudgetStopped('Next reservation cannot fit the authorized cumulative budget')
            ident=str(uuid.uuid4())
            rows.append(dict(attempt_id=ident,request_hash=request_hash,status='reserved',reserved_usd=self.reserve_amount,
                preflight=preflight,cost_mode=preflight.get('backend','Claude Code')+' client-side estimate; not a guaranteed provider billing ceiling'))
            dump(self.path,rows)
        return ident

    def settle(self,ident,result):
        cost=result.get('total_cost_usd')
        if not isinstance(cost,(int,float)) or not math.isfinite(cost) or cost<0:
            return # No reliable usage receipt: keep the full reservation.
        with exclusive_lock(self.lock):
            rows,_,_=self.state();row=next(r for r in rows if r['attempt_id']==ident)
            row.update(status='settled',reported_cost_usd=cost,usage=result.get('usage'),model_usage=result.get('modelUsage'),
                       reservation_exceeded=cost>row['reserved_usd'])
            dump(self.path,rows)

    def cancel_before_launch(self,ident):
        with exclusive_lock(self.lock):
            rows,_,_=self.state();row=next(r for r in rows if r['attempt_id']==ident)
            row.update(status='settled',reported_cost_usd=0,usage=None,reason='Operating-system launch failed before a model process started')
            dump(self.path,rows)

    def summary(self):
        _,spent,held=self.state()
        return dict(authorized_cumulative_budget_usd=self.cap,reported_cost_usd=spent,outstanding_reservations_usd=held,
            committed_estimate_usd=spent+held,headroom_usd=self.cap-spent-held,
            full_cli_precount_available=False,provider_hard_cost_guarantee=False,
            limit='CLI caps can be exceeded within a request and estimates may differ from billing. Strict pre-counting requires the existing direct API path.')
