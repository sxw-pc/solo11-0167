from .store import open_db
from .evidence import at,money,read_source,unique_event
from .vault import release_slot

def originate(path,file):
    with open_db(path) as db,db:
        sid,d,replay=read_source(db,'loan',file)
        if unique_event(db,'loan',d['id'],sid):return {'id':d['id'],'replayed':True}
        asset=db.execute('SELECT * FROM asset WHERE asset_id=?',(d['asset_id'],)).fetchone()
        if not asset or asset['status']!='sealed' or asset['owner']!=d['customer']:raise ValueError('asset_not_eligible')
        issued,due=at(d['issued_at']),at(d['due_at'])
        if due<=issued:raise ValueError('loan_window_invalid')
        own=db.execute("SELECT * FROM ownership WHERE asset_id=? AND status='verified' AND valid_from<=? AND valid_to>? ORDER BY valid_from DESC LIMIT 1",(d['asset_id'],issued,issued)).fetchone()
        if not own:raise ValueError('ownership_not_verified')
        appraisal=db.execute("SELECT * FROM appraisal WHERE id=? AND asset_id=? AND status='valid' AND valid_to>?",(d['appraisal_id'],d['asset_id'],issued)).fetchone()
        if not appraisal:raise ValueError('appraisal_expired')
        principal=money(d['principal']);interest=money(d['interest'])
        if principal>appraisal['amount_cents']*appraisal['discount_percent']//100:raise ValueError('loan_ceiling_exceeded')
        if db.execute("SELECT 1 FROM loan WHERE asset_id=? AND status IN ('active','extended','disputed')",(d['asset_id'],)).fetchone():raise ValueError('duplicate_active_loan')
        db.execute('INSERT INTO loan VALUES(?,?,?,?,?,?,?,?,?)',(d['id'],d['asset_id'],principal,interest,due,d['customer'],'active',appraisal['id'],sid))
        return {'id':d['id'],'status':'active','due_cents':principal+interest}

def pay(path,file):
    """Record a payment.

    ``payer``/``relationship`` are optional and default to the borrower.  A third
    party (for example an heir advancing the money) may settle the debt; paying
    never transfers ownership or pickup authority.
    """
    with open_db(path) as db,db:
        sid,d,replay=read_source(db,'payment',file)
        if unique_event(db,'payment',d['id'],sid):return {'id':d['id'],'replayed':True}
        loan=db.execute('SELECT * FROM loan WHERE id=?',(d['loan_id'],)).fetchone()
        if not loan or loan['status'] not in ('active','extended','disputed'):raise ValueError('loan_not_payable')
        amount=money(d['amount']);moment=at(d['at'])
        payer=d.get('payer',loan['customer'])
        dead=db.execute("SELECT deceased_at FROM customer WHERE customer=? AND status='deceased' "
                        "AND deceased_at<=? ORDER BY verified_at DESC LIMIT 1",
                        (loan['customer'],moment)).fetchone()
        if payer==loan['customer'] and dead:
            raise ValueError('payer_must_be_living_or_representative')
        paid=db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payment WHERE loan_id=?',(d['loan_id'],)).fetchone()[0]
        if paid+amount>loan['principal_cents']+loan['interest_cents']:raise ValueError('overpayment')
        db.execute('INSERT INTO payment VALUES(?,?,?,?,?)',(d['id'],d['loan_id'],amount,moment,sid))
        db.execute('INSERT INTO payment_payer VALUES(?,?,?,?,?)',
                   (d['id'],d['loan_id'],payer,d.get('relationship','self'),sid))
        return {'id':d['id'],'outstanding_cents':loan['principal_cents']+loan['interest_cents']-paid-amount,
                'payer':payer}

def extend(path,file):
    with open_db(path) as db,db:
        sid,d,replay=read_source(db,'extension',file)
        if unique_event(db,'extension',d['id'],sid):return {'id':d['id'],'replayed':True}
        loan=db.execute('SELECT * FROM loan WHERE id=?',(d['loan_id'],)).fetchone()
        if not loan or loan['status'] not in ('active','extended'):raise ValueError('loan_not_extendable')
        moment,new_due=at(d['at']),at(d['new_due_at'])
        if moment>=loan['due_at'] or new_due<=loan['due_at'] or d['customer_actor']==d['shop_actor']:
            raise ValueError('invalid_extension')
        db.execute('INSERT INTO extension VALUES(?,?,?,?,?,?,?)',(d['id'],d['loan_id'],new_due,d['customer_actor'],d['shop_actor'],moment,sid))
        db.execute("UPDATE loan SET due_at=?,status='extended' WHERE id=?",(new_due,d['loan_id']))
        return {'id':d['id'],'new_due_at':new_due}

def notice(path,file):
    with open_db(path) as db,db:
        sid,d,replay=read_source(db,'notice',file)
        if unique_event(db,'notice',d['id'],sid):return {'id':d['id'],'replayed':True}
        loan=db.execute('SELECT * FROM loan WHERE id=?',(d['loan_id'],)).fetchone()
        if not loan:raise ValueError('unknown_loan')
        delivered,grace=at(d['delivered_at']),at(d['grace_until'])
        if delivered<loan['due_at'] or grace<=delivered or d['recipient']!=loan['customer']:
            raise ValueError('invalid_notice')
        db.execute('INSERT INTO notice VALUES(?,?,?,?,?,?)',(d['id'],d['loan_id'],delivered,grace,d['recipient'],sid))
        return {'id':d['id'],'grace_until':grace}

def _lawful_delivery(db,loan_id):
    return db.execute("SELECT * FROM physical_handover WHERE loan_id=? AND accepted=1 "
                      "AND status='delivered' ORDER BY at,id DESC LIMIT 1",(loan_id,)).fetchone()

def decide(path,file):
    """Debt/deadline decision.

    For the legacy living-borrower counter flow the old combined behaviour is
    kept.  Once the borrower is deceased, or a handoff route has been fixed, this
    decision no longer moves the asset: physical release only happens through
    ``redemption_handoff.confirm_handover`` with a countersigned receipt.
    """
    with open_db(path) as db,db:
        sid,d,replay=read_source(db,'decision',file)
        if replay:
            old=db.execute('SELECT status,reason FROM decision WHERE source_id=?',(sid,)).fetchone()
            if old:return {'status':old['status'],'reason':old['reason'],'replayed':True}
        loan=db.execute('SELECT * FROM loan WHERE id=?',(d['loan_id'],)).fetchone()
        if not loan:raise ValueError('unknown_loan')
        moment=at(d['at']);kind=d['kind']
        asset=db.execute('SELECT status FROM asset WHERE asset_id=?',(loan['asset_id'],)).fetchone()[0]
        paid=db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payment WHERE loan_id=?',(d['loan_id'],)).fetchone()[0]
        outstanding=loan['principal_cents']+loan['interest_cents']-paid
        plan=db.execute('SELECT 1 FROM handoff_plan WHERE loan_id=?',(d['loan_id'],)).fetchone()
        deceased=db.execute("SELECT 1 FROM customer WHERE customer=? AND status='deceased'",
                            (loan['customer'],)).fetchone()
        if kind=='redeem':
            if outstanding:status,reason='blocked','unpaid_balance'
            elif _lawful_delivery(db,d['loan_id']):status,reason='approved','settled_and_delivered'
            elif asset!='sealed':status,reason='blocked','custody_disputed'
            elif deceased or plan:status,reason='blocked','heir_handoff_required'
            else:status,reason='approved','paid_and_sealed'
        elif kind=='forfeit':
            n=db.execute('SELECT * FROM notice WHERE loan_id=? ORDER BY grace_until DESC LIMIT 1',(d['loan_id'],)).fetchone()
            if outstanding==0:status,reason='blocked','already_paid'
            elif asset!='sealed':status,reason='blocked','custody_disputed'
            elif not n or moment<n['grace_until']:status,reason='blocked','notice_or_grace_missing'
            else:status,reason='approved','lawful_expiry'
        else:raise ValueError('unknown_decision_kind')
        db.execute('INSERT INTO decision(loan_id,kind,status,reason,at,source_id) VALUES(?,?,?,?,?,?)',(d['loan_id'],kind,status,reason,moment,sid))
        if status=='approved':
            db.execute('UPDATE loan SET status=? WHERE id=?',('redeemed' if kind=='redeem' else 'forfeited',d['loan_id']))
            # Legacy counter flow only: no inheritance/handoff evidence exists.
            if kind=='redeem' and not _lawful_delivery(db,d['loan_id']):
                release_slot(db,loan['asset_id'])
        elif reason=='settled_and_delivered':
            db.execute("UPDATE loan SET status='redeemed' WHERE id=?",(d['loan_id'],))
        return {'status':status,'reason':reason,'outstanding_cents':outstanding}
