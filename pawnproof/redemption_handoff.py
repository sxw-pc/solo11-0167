"""离线继承/代理赎回采录与不可逆实物交付。

本模块把原本被合成一步的三件事拆开：

1. ``intake_*``/``document`` 等只采录证据（客户身故状态、继承人份额、授权/撤回、
   法院指令、柜台核身、保管点登记），不改变质物占有。
2. ``claim_status`` 按**实际交付时点**重放全部已收证据，回答“此刻谁有权取得
   现实占有”。文件可乱序到达；只有目录元数据、没有正文的文件不算授权。
3. ``fix_route`` / ``dispatch`` / ``confirm_handover`` / ``reconcile`` 负责不可
   逆实物交接。路线必须在打开库位前固定；打开库位前先落一条写前意图
   （``dispatch_intent``）；外部收件回执可能晚于数据库落库。``asset.status``
   绝不因为“出库”而变成 released，只有持双人签收与核身的有效回执才释放。

所有证据行只追加。晚到撤回、错人签收、交付中断只新增事实，不删除旧事实；
``reconcile`` 负责把物理事实与数据库裁决重新对齐，并开出争议/追偿线索。
"""
import hashlib, json

from .store import open_db
from .evidence import at, read_source
from . import vault

DOC_KINDS = ('authorization', 'proxy', 'revocation', 'court_order',
             'custody_application', 'custody_receipt')


def _digest(doc):
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# 采录：客户身份状态 / 继承人份额
# ---------------------------------------------------------------------------

def customer_status(path, file):
    with open_db(path) as db, db:
        sid, d, replay = read_source(db, 'customer', file)
        if replay:
            return {'customer': d['customer'], 'replayed': True}
        customer = d.get('customer')
        if not customer:
            raise ValueError('customer_required')
        status = d.get('status')
        if status not in ('alive', 'deceased'):
            raise ValueError('unknown_customer_status')
        verified = at(d['verified_at'])
        deceased = at(d['deceased_at']) if d.get('deceased_at') else ''
        if status == 'deceased' and not deceased:
            raise ValueError('deceased_time_required')
        # A later "alive" statement cannot undo an already-recorded death.
        prior = db.execute("SELECT deceased_at FROM customer WHERE customer=? AND status='deceased'",
                           (customer,)).fetchone()
        if prior:
            if status == 'alive':
                raise ValueError('death_cannot_be_reversed')
            deceased = deceased or prior['deceased_at']
        db.execute('INSERT OR REPLACE INTO customer VALUES(?,?,?,?,?,?)',
                   (customer, status, verified, d.get('authority', ''), deceased, sid))
        _reconcile(db)
        return {'customer': customer, 'status': status, 'deceased_at': deceased or None}


def heir_share(path, file):
    with open_db(path) as db, db:
        sid, d, replay = read_source(db, 'heir', file)
        items = d.get('heirs')
        if items is None:
            items = [d]
        added = []
        for item in items:
            old = db.execute('SELECT * FROM heir WHERE id=?', (item['id'],)).fetchone()
            if old:
                if old['source_id'] != sid:
                    raise ValueError('event_id_conflict')
                continue
            if item.get('customer', d.get('customer')) != d.get('customer'):
                raise ValueError('heir_customer_mismatch')
            num, den = int(item['share_num']), int(item['share_den'])
            if num <= 0 or den <= 0 or num > den:
                raise ValueError('share_out_of_range')
            db.execute('INSERT INTO heir VALUES(?,?,?,?,?,?,?,?,?)',
                       (item['id'], d['customer'], item['heir_id'], num, den,
                        at(item.get('deceased_at') or d['deceased_at']),
                        at(item.get('asserted_at') or d['asserted_at']),
                        item.get('status', 'asserted'), sid))
            added.append(item['id'])
        _reconcile(db)
        return {'ids': added, 'replayed': not added}


# ---------------------------------------------------------------------------
# 采录：授权 / 撤回 / 法院指令 / 正文补件 / 核身 / 保管点
# ---------------------------------------------------------------------------

def _insert_document(db, sid, d):
    doc_id = d['id']
    old = db.execute('SELECT * FROM inheritance_document WHERE id=?', (doc_id,)).fetchone()
    if old:
        if old['source_id'] != sid:
            raise ValueError('event_id_conflict')
        return old, True
    kind = d.get('kind')
    if kind not in DOC_KINDS:
        raise ValueError('unknown_document_kind')
    if not d.get('received_at'):
        raise ValueError('document_received_at_required')
    body_present = bool(d.get('body_present'))
    body_sha = d.get('body_sha') or ''
    if body_present and not body_sha:
        raise ValueError('document_body_digest_required')
    if body_sha and not body_present:
        raise ValueError('document_body_missing')
    if kind in ('authorization', 'proxy'):
        if not d.get('grantor') or not d.get('representative'):
            raise ValueError('authorization_parties_required')
        if d.get('scope', 'redeem') != 'redeem':
            raise ValueError('authorization_scope_not_redeem')
    if kind == 'revocation':
        if not d.get('revokes') or not d.get('effective_from'):
            raise ValueError('revocation_target_and_effect_required')
    if kind == 'court_order':
        if not d.get('court_effect') or not d.get('authority'):
            raise ValueError('court_effect_required')
    # 只有目录元数据而没有正文的文件不是授权；state 显式标出。
    state = 'complete' if body_present else 'metadata_only'
    db.execute('INSERT INTO inheritance_document(id,kind,customer,asset_id,loan_id,grantor,'
               'representative,scope,effective_from,effective_to,received_at,revokes,'
               'court_effect,court_priority,authority,custody_point,body_sha,body_present,'
               'state,detail,source_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (doc_id, kind, d.get('customer', ''), d.get('asset_id', ''),
                d.get('loan_id', ''), d.get('grantor', ''), d.get('representative', ''),
                d.get('scope', ''), at(d['effective_from']) if d.get('effective_from') else '',
                at(d['effective_to']) if d.get('effective_to') else '',
                at(d['received_at']) if d.get('received_at') else '',
                d.get('revokes', ''), d.get('court_effect', ''),
                int(d.get('court_priority', 0)), d.get('authority', ''),
                d.get('custody_point', ''), body_sha, 1 if body_present else 0,
                state, '', sid))
    return db.execute('SELECT * FROM inheritance_document WHERE id=?', (doc_id,)).fetchone(), False


def document(path, file):
    with open_db(path) as db, db:
        sid, d, replay = read_source(db, 'inheritance_document', file)
        old, existed = _insert_document(db, sid, d)
        _reconcile(db)
        return {'id': old['id'], 'state': old['state'], 'replayed': replay or existed}


def document_body(path, file):
    """正文可单独后到；目录已到但正文未到时不能当成授权。"""
    with open_db(path) as db, db:
        sid, d, replay = read_source(db, 'document_body', file)
        old = db.execute('SELECT * FROM document_body WHERE document_id=?',
                         (d['document_id'],)).fetchone()
        if old:
            if old['source_id'] != sid:
                raise ValueError('event_id_conflict')
            return {'document_id': d['document_id'], 'replayed': True}
        doc = db.execute('SELECT * FROM inheritance_document WHERE id=?',
                         (d['document_id'],)).fetchone()
        if not doc:
            raise ValueError('unknown_document')
        if doc['body_sha'] and doc['body_sha'] != d['body_sha']:
            raise ValueError('document_body_mismatch')
        db.execute('INSERT INTO document_body VALUES(?,?,?,?)',
                   (d['document_id'], d['body_sha'], at(d['received_at']), sid))
        db.execute("UPDATE inheritance_document SET body_present=1,state='complete',body_sha=? "
                   "WHERE id=?", (d['body_sha'], d['document_id']))
        _reconcile(db)
        return {'document_id': d['document_id'], 'state': 'complete'}


def counter_check(path, file):
    with open_db(path) as db, db:
        sid, d, replay = read_source(db, 'counter_verification', file)
        old = db.execute('SELECT * FROM counter_verification WHERE id=?', (d['id'],)).fetchone()
        if old:
            if old['source_id'] != sid:
                raise ValueError('event_id_conflict')
            return {'id': d['id'], 'replayed': True}
        db.execute('INSERT INTO counter_verification VALUES(?,?,?,?,?,?,?)',
                   (d['id'], d['subject'], d['verifier'], at(d['at']),
                    1 if d.get('matched') else 0, d.get('method', 'id_document'), sid))
        return {'id': d['id'], 'matched': bool(d.get('matched'))}


def custody_point(path, file):
    """登记法院/中立保管点；它是 neutral_custody 路线的唯一交付对象。"""
    with open_db(path) as db, db:
        sid, d, replay = read_source(db, 'custody_point', file)
        if db.execute('SELECT 1 FROM vault_slot WHERE slot_id=?', (d['slot_id'],)).fetchone():
            return {'slot_id': d['slot_id'], 'replayed': True}
        db.execute('INSERT INTO vault_slot VALUES(?,?,NULL)',
                   (d['slot_id'], int(d.get('capacity', 1))))
        db.execute('INSERT INTO handoff_event(plan_id,phase,state,at,detail,source_id) '
                   'VALUES(?,?,?,?,?,?)',
                   ('', 'custody_point', 'registered', at(d['registered_at']),
                    json.dumps({'slot_id': d['slot_id'], 'name': d['name']},
                               ensure_ascii=False), sid))
        return {'slot_id': d['slot_id'], 'registered': True}


# ---------------------------------------------------------------------------
# 权利核验：按交付时点重放证据，而不是按导入顺序
# ---------------------------------------------------------------------------

def _docs(db):
    return [dict(x) for x in db.execute('SELECT * FROM inheritance_document ORDER BY id')]


def _effective_docs(db, moment, evidence_cutoff):
    """在 moment 生效、且在 evidence_cutoff 前已知的完整文件。

    evidence_cutoff=None 表示用“今天已收到的全部证据”回溯判断。
    """
    out = []
    for d in _docs(db):
        if not d['body_present']:
            continue
        if evidence_cutoff is not None and d['received_at'] and d['received_at'] > evidence_cutoff:
            continue
        if d['effective_from'] and d['effective_from'] > moment:
            continue
        if d['effective_to'] and d['effective_to'] <= moment:
            continue
        out.append(d)
    return out


def shares(db, customer, moment):
    rows = [dict(x) for x in db.execute(
        'SELECT * FROM heir WHERE customer=? AND asserted_at<=? ORDER BY asserted_at,id',
        (customer, moment))]
    latest = {}
    for r in rows:
        latest[r['heir_id']] = r
    return latest


def _court_state(db, moment, cutoff):
    docs = [d for d in _effective_docs(db, moment, cutoff) if d['kind'] == 'court_order']
    docs.sort(key=lambda d: (d['court_priority'], d['received_at'] or '', d['id']))
    forced = None
    permits = []
    for d in docs:
        if d['court_effect'] in ('impound', 'custody_order', 'exclusive_custodian'):
            forced = d
        elif d['court_effect'] in ('permit_neutral_custody', 'permit_voluntary_custody'):
            permits.append(d)
    return forced, permits


def claim_status(db, loan_id, moment, evidence_cutoff='__same__'):
    """回答“此刻谁有权取得结清质物的现实占有”。

    - 付款人 ≠ 所有人：付款只清债，不产生领取权。
    - 有效份额合计必须为 1。
    - 代表授权必须覆盖该继承人的份额，且在 moment 生效、未被有效撤回。
    - 法院强制指令优先级高于私人委托；强制保管时任何人不得单领。

    ``db`` 可以是已打开的连接，也可以是数据库路径（只读查询）。
    """
    if not hasattr(db, 'execute'):
        with open_db(db) as con:
            return claim_status(con, loan_id, moment, evidence_cutoff)
    if evidence_cutoff == '__same__':
        evidence_cutoff = moment
    loan = db.execute('SELECT * FROM loan WHERE id=?', (loan_id,)).fetchone()
    if not loan:
        raise ValueError('unknown_loan')
    customer = loan['customer']
    cust = db.execute('SELECT * FROM customer WHERE customer=? AND verified_at<=? '
                      'ORDER BY verified_at DESC LIMIT 1', (customer, moment)).fetchone()
    deceased = bool(cust and cust['status'] == 'deceased')
    if not deceased:
        # 原柜台流程：在生借款人本人仍是有权领取人。付款人身份不改变这一点。
        forced, permits = _court_state(db, moment, evidence_cutoff)
        if forced:
            point = forced['custody_point'] or forced['authority']
            return {'status': 'court_custody_required', 'eligible': [],
                    'court_order': forced['id'], 'forced_court_order': forced['id'],
                    'custody_recipient': point, 'deceased': False, 'heir_detail': [],
                    'court_orders': [d['id'] for d in permits],
                    'detail': 'court_priority_overrides_private_authority'}
        return {'status': 'borrower_eligible', 'eligible': [customer], 'heir_detail': [],
                'court_orders': [d['id'] for d in permits], 'forced_court_order': None,
                'custody_recipient': None, 'deceased': False,
                'detail': 'living_borrower_keeps_pickup_authority'}

    forced, permits = _court_state(db, moment, evidence_cutoff)
    if forced:
        point = forced['custody_point'] or forced['authority']
        return {'status': 'court_custody_required', 'eligible': [],
                'court_order': forced['id'], 'forced_court_order': forced['id'],
                'custody_recipient': point, 'deceased': deceased,
                'heir_detail': [], 'court_orders': [d['id'] for d in permits],
                'detail': 'court_priority_overrides_private_authority'}

    docs = _effective_docs(db, moment, evidence_cutoff)
    revoked = {}
    for d in docs:
        if d['kind'] == 'revocation' and d['revokes']:
            revoked.setdefault(d['revokes'], []).append(d)

    def live(doc):
        return not any(r['effective_from'] and r['effective_from'] <= moment
                       for r in revoked.get(doc['id'], []))

    heir_rows = shares(db, customer, moment)
    # person -> 可代表的有效份额合计 (num, den)
    coverage = {}
    detail = []
    total_num, total_den = 0, 1
    for heir_id, row in sorted(heir_rows.items()):
        if row['status'] != 'asserted':
            continue
        num, den = row['share_num'], row['share_den']
        total_num, total_den = total_num * den + num * total_den, total_den * den
        coverage.setdefault(heir_id, [0, 1])
        n, dd = coverage[heir_id]
        coverage[heir_id] = [n * den + num * dd, dd * den]
        granted = []
        for doc in docs:
            if doc['kind'] not in ('authorization', 'proxy'):
                continue
            if doc['grantor'] != heir_id or not live(doc):
                continue
            rep = doc['representative']
            n, dd = coverage.setdefault(rep, [0, 1])
            coverage[rep] = [n * den + num * dd, dd * den]
            granted.append(rep)
        detail.append({'heir': heir_id, 'share': f'{num}/{den}',
                       'granted_to': sorted(set(granted))})
    complete = total_num == total_den
    eligible = sorted(p for p, (n, dd) in coverage.items() if complete and n == dd)
    if not complete:
        status = 'share_incomplete'
    elif eligible:
        status = 'eligible'
    else:
        status = 'no_authorized_representative'
    return {'status': status, 'eligible': eligible, 'heir_detail': detail,
            'court_orders': [d['id'] for d in permits], 'forced_court_order': None,
            'custody_recipient': None, 'deceased': deceased,
            'detail': 'shares_and_authority_checked_at_delivery_time'}


def neutral_custody_status(db, loan_id, moment, evidence_cutoff='__same__'):
    if evidence_cutoff == '__same__':
        evidence_cutoff = moment
    forced, permits = _court_state(db, moment, evidence_cutoff)
    permit = forced or (permits[-1] if permits else None)
    if not permit:
        return {'status': 'no_court_permit', 'custody_point': None, 'court_order': None}
    point = permit['custody_point'] or permit['authority']
    registered = db.execute('SELECT 1 FROM vault_slot WHERE slot_id=?', (point,)).fetchone()
    if not registered:
        return {'status': 'custody_point_unregistered', 'custody_point': point,
                'court_order': permit['id']}
    return {'status': 'permitted', 'custody_point': point, 'court_order': permit['id'],
            'forced': bool(forced)}


def _late_revocations_for(db, recipient, moment):
    """影响 recipient 的、生效早于 moment 但送达晚于 moment 的撤回。"""
    late = []
    for d in _docs(db):
        if d['kind'] != 'revocation' or not d['body_present']:
            continue
        if not (d['effective_from'] and d['effective_from'] <= moment
                and d['received_at'] and d['received_at'] > moment):
            continue
        if _revocation_target(db, d) == recipient:
            late.append(d)
    return late


def _revocation_target(db, doc):
    """撤回所指向的授权书的受任人；找不到授权书时为空。"""
    target = db.execute('SELECT * FROM inheritance_document WHERE id=?',
                        (doc['revokes'],)).fetchone()
    if not target or target['kind'] not in ('authorization', 'proxy'):
        return None
    return target['representative']


# ---------------------------------------------------------------------------
# 路线固定
# ---------------------------------------------------------------------------

def fixed_plan(db, loan_id):
    return db.execute('SELECT * FROM handoff_plan WHERE loan_id=? ORDER BY fixed_at,id',
                      (loan_id,)).fetchone()


def fix_route(path, file):
    """在打开库位前固定唯一路线；固定后同贷款不得再改路线。"""
    with open_db(path) as db, db:
        sid, d, replay = read_source(db, 'handoff_plan', file)
        old = db.execute('SELECT * FROM handoff_plan WHERE id=?', (d['id'],)).fetchone()
        if old:
            if old['source_id'] != sid:
                raise ValueError('event_id_conflict')
            return {'id': old['id'], 'route': old['route'], 'replayed': True}
        loan = db.execute('SELECT * FROM loan WHERE id=?', (d['loan_id'],)).fetchone()
        if not loan:
            raise ValueError('unknown_loan')
        if db.execute('SELECT 1 FROM handoff_plan WHERE loan_id=?', (d['loan_id'],)).fetchone():
            raise ValueError('route_already_fixed')
        if db.execute("SELECT 1 FROM physical_handover WHERE loan_id=?",
                      (d['loan_id'],)).fetchone():
            raise ValueError('route_must_precede_physical_movement')
        route, recipient = d['route'], d['recipient']
        fixed_at = at(d['at'])
        if route == 'direct_heir':
            if d.get('recipient_kind') != 'heir':
                raise ValueError('direct_route_requires_heir')
            claim = claim_status(db, d['loan_id'], fixed_at, evidence_cutoff=fixed_at)
            if claim['status'] == 'court_custody_required':
                raise ValueError('court_priority_blocks_direct_pickup')
            if recipient not in claim['eligible']:
                raise ValueError('recipient_not_authorized_at_route_fix')
            court_order = ''
        elif route == 'neutral_custody':
            if d.get('recipient_kind') != 'custody_point':
                raise ValueError('neutral_route_requires_custody_point')
            court = neutral_custody_status(db, d['loan_id'], fixed_at, evidence_cutoff=fixed_at)
            if court['status'] != 'permitted':
                raise ValueError(court['status'])
            if recipient != court['custody_point']:
                raise ValueError('recipient_is_not_court_custody_point')
            court_order = court['court_order']
        else:
            raise ValueError('unknown_handoff_route')
        slot = db.execute('SELECT occupied_asset FROM vault_slot WHERE occupied_asset=?',
                          (loan['asset_id'],)).fetchone()
        if not slot:
            raise ValueError('asset_not_in_vault')
        db.execute('INSERT INTO handoff_plan VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (d['id'], d['loan_id'], loan['asset_id'], route, recipient,
                    d['recipient_kind'], court_order, d.get('slot_id', ''),
                    fixed_at, fixed_at, 'fixed', sid))
        db.execute('INSERT INTO handoff_event(plan_id,phase,state,at,detail,source_id) '
                   'VALUES(?,?,?,?,?,?)',
                   (d['id'], 'route', 'fixed', fixed_at,
                    json.dumps({'route': route, 'recipient': recipient}, ensure_ascii=False), sid))
        return {'id': d['id'], 'route': route, 'recipient': recipient, 'status': 'fixed'}


# ---------------------------------------------------------------------------
# 写前意图与实物抬出
# ---------------------------------------------------------------------------

def dispatch(path, file, simulate=None):
    """写前意图先落库，再抬出库位。

    ``simulate='crash_after_intent'`` 只在测试/演练中模拟“意图已提交、物理
    抬出尚未发生”的崩溃点，用于验证恢复协议。
    """
    with open_db(path) as db, db:
        sid, d, replay = read_source(db, 'dispatch', file)
        old = db.execute('SELECT * FROM dispatch_intent WHERE id=?', (d['id'],)).fetchone()
        if old:
            if old['source_id'] != sid:
                raise ValueError('event_id_conflict')
            return {'id': old['id'], 'status': old['status'], 'replayed': True}
        plan = db.execute('SELECT * FROM handoff_plan WHERE id=?', (d['plan_id'],)).fetchone()
        if not plan:
            raise ValueError('route_not_fixed')
        loan = db.execute('SELECT * FROM loan WHERE id=?', (plan['loan_id'],)).fetchone()
        paid = db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payment WHERE loan_id=?',
                          (plan['loan_id'],)).fetchone()[0]
        outstanding = loan['principal_cents'] + loan['interest_cents'] - paid
        if outstanding:
            raise ValueError('loan_not_settled')
        asset = db.execute('SELECT * FROM asset WHERE asset_id=?', (plan['asset_id'],)).fetchone()
        if not asset or asset['status'] != 'sealed':
            raise ValueError('asset_not_sealed')
        if not db.execute('SELECT 1 FROM vault_slot WHERE occupied_asset=?',
                          (plan['asset_id'],)).fetchone():
            raise ValueError('asset_not_in_slot')
        if d['actor_a'] == d['actor_b'] or not d['actor_a'] or not d['actor_b']:
            raise ValueError('independent_vault_actors_required')
        moment = at(d['at'])
        if plan['fixed_at'] > moment:
            raise ValueError('dispatch_before_route_fixed')
        # 打开库位前按当前已收证据复核一次权利；授权在固定路线后失效则
        # 不得继续抬出（库位里的质物仍是封存的）。
        if plan['route'] == 'direct_heir':
            claim = claim_status(db, plan['loan_id'], moment, evidence_cutoff=moment)
            if plan['recipient'] not in claim['eligible']:
                raise ValueError('recipient_authority_lapsed_before_dispatch')
        else:
            court = neutral_custody_status(db, plan['loan_id'], moment, evidence_cutoff=moment)
            if court['status'] != 'permitted' or plan['recipient'] != court['custody_point']:
                raise ValueError('court_permit_lapsed_before_dispatch')
        # 1) 写前意图：必须先于任何物理动作持久化。
        db.execute('INSERT INTO dispatch_intent VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (d['id'], plan['id'], plan['loan_id'], plan['asset_id'],
                    plan['slot_id'], d['actor_a'], d['actor_b'], plan['recipient'],
                    plan['recipient_kind'], plan['route'], moment, 'intent', sid))
        db.execute('INSERT INTO handoff_event(plan_id,phase,state,at,detail,source_id) '
                   'VALUES(?,?,?,?,?,?)',
                   (plan['id'], 'dispatch', 'intent', moment,
                    json.dumps({'dispatch_id': d['id']}, ensure_ascii=False), sid))
        db.commit()
        if simulate == 'crash_after_intent':
            # 意图已提交、尚未抬出；恢复协议必须能安全回退为 sealed。
            return {'id': d['id'], 'status': 'intent', 'simulated_crash': True}
        # 2) 物理抬出：库位清空只表示“离开库位”，不表示交付。
        vault.lift_asset(db, plan['asset_id'], moment, 'shop-vault',
                         'dispatch_lift', d['id'], sid)
        db.execute("UPDATE dispatch_intent SET status='lifted' WHERE id=?", (d['id'],))
        db.execute('INSERT INTO handoff_event(plan_id,phase,state,at,detail,source_id) '
                   'VALUES(?,?,?,?,?,?)',
                   (plan['id'], 'dispatch', 'lifted', moment,
                    json.dumps({'dispatch_id': d['id'], 'holder': 'shop-vault'},
                               ensure_ascii=False), sid))
        _reconcile(db)
        return {'id': d['id'], 'status': 'lifted', 'holder': 'shop-vault',
                'asset_status': 'held_at_shop'}


# ---------------------------------------------------------------------------
# 外部收件回执与交付确认
# ---------------------------------------------------------------------------

def _verify_counter(db, subject, verification_id, moment):
    row = db.execute('SELECT * FROM counter_verification WHERE id=?', (verification_id,)).fetchone()
    if not row or row['subject'] != subject or not row['matched'] or row['at'] > moment:
        raise ValueError('recipient_identity_unverified')
    return row


def confirm_handover(path, file, finalize=True):
    """记录外部收件回执；``finalize=False`` 模拟“回执已存在、数据库落库失败”。

    回执行一旦写入即为物理事实，之后只追加争议，不删除、不改写。
    """
    with open_db(path) as db, db:
        sid, d, replay = read_source(db, 'physical_handover', file)
        intent = db.execute('SELECT * FROM dispatch_intent WHERE id=?',
                            (d['dispatch_id'],)).fetchone()
        if not intent:
            raise ValueError('unknown_dispatch')
        old = db.execute('SELECT * FROM physical_handover WHERE id=?', (d['id'],)).fetchone()
        if old:
            if old['source_id'] != sid:
                raise ValueError('event_id_conflict')
            return {'id': old['id'], 'status': old['status'], 'replayed': True}
        plan = db.execute('SELECT * FROM handoff_plan WHERE id=?', (intent['plan_id'],)).fetchone()
        moment = at(d['at'])
        if moment < intent['at']:
            raise ValueError('receipt_before_dispatch')
        if d['recipient'] != plan['recipient']:
            # 交付对象必须唯一；换人不是同一笔放行。
            raise ValueError('recipient_mismatch')
        if d['actor_a'] == d['actor_b'] or not d['actor_a'] or not d['actor_b']:
            raise ValueError('independent_vault_signatures_required')
        accepted = bool(d.get('accepted'))
        verification = _verify_counter(db, d['recipient'], d['counter_verification_id'], moment)
        status, reason, lawful, custodian = 'pending', '', False, 'shop-vault'
        registration = ''
        if not accepted:
            status, reason = 'refused_by_recipient', d.get('reason', 'external_recipient_refused')
        elif plan['route'] == 'neutral_custody':
            registration = d.get('court_registration_id', '')
            receipt = db.execute('SELECT * FROM counter_verification WHERE id=?',
                                 (registration,)).fetchone()
            if not receipt or receipt['method'] != 'court_receipt' or not receipt['matched'] \
                    or receipt['subject'] != d['recipient'] or receipt['at'] > moment:
                raise ValueError('court_custody_receipt_required')
            status, lawful, custodian = 'delivered', True, d['recipient']
        else:
            known = claim_status(db, intent['loan_id'], moment, evidence_cutoff=moment)
            if d['recipient'] in known['eligible']:
                status, lawful, custodian = 'delivered', True, d['recipient']
            else:
                status, reason = 'unauthorized_recipient', 'recipient_not_authorized'
                lawful, custodian = False, d['recipient']
        receipt_sha = d.get('receipt_sha') or _digest(d)
        if db.execute('SELECT 1 FROM physical_handover WHERE log_sha=?',
                      (receipt_sha,)).fetchone():
            raise ValueError('receipt_sha_conflict')
        db.execute('INSERT INTO physical_handover(id,plan_id,dispatch_id,loan_id,asset_id,route,'
                   'recipient,recipient_kind,accepted,status,reason,actual_custodian,'
                   'holding_party,vault_actor_a,vault_actor_b,counter_verification_id,'
                   'court_registration_id,receipt_sha,lifted_from_slot,finalized,at,detail,'
                   'log_sha,source_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (d['id'], plan['id'], intent['id'], intent['loan_id'], intent['asset_id'],
                    plan['route'], d['recipient'], plan['recipient_kind'], 1 if accepted else 0,
                    status, reason, custodian, custodian, d['actor_a'], d['actor_b'],
                    verification['id'], registration, receipt_sha, 1, 0,
                    moment, d.get('detail', ''), receipt_sha, sid))
        if not finalize:
            return {'id': d['id'], 'status': status, 'finalized': False}
        _finalize(db, d['id'])
        _reconcile(db)
        return {'id': d['id'], 'status': db.execute(
            'SELECT status FROM physical_handover WHERE id=?', (d['id'],)).fetchone()[0],
            'finalized': True, 'actual_custodian': custodian}


def _finalize(db, handover_id):
    """把已存在的回执落到资产/贷款/库位/位置账；只做一次。"""
    h = db.execute('SELECT * FROM physical_handover WHERE id=?', (handover_id,)).fetchone()
    if not h or h['finalized']:
        return
    db.execute('UPDATE physical_handover SET finalized=1 WHERE id=?', (handover_id,))
    if h['accepted'] and h['status'] == 'delivered':
        vault.release_asset(db, h['asset_id'], h['at'], h['actual_custodian'],
                            h['id'], h['source_id'])
        db.execute("UPDATE dispatch_intent SET status='delivered' WHERE id=?",
                   (h['dispatch_id'],))
        db.execute('INSERT INTO handoff_event(plan_id,phase,state,at,detail,source_id) '
                   'VALUES(?,?,?,?,?,?)',
                   (h['plan_id'], 'handover', 'delivered', h['at'],
                    json.dumps({'recipient': h['actual_custodian'], 'handover_id': h['id']},
                               ensure_ascii=False), h['source_id']))
        _maybe_redeem(db, h['loan_id'])
    elif h['accepted']:
        # 错人/争议签收：物已离店，位置账记录实际持有人，但绝不报告“合法领取”。
        db.execute("UPDATE dispatch_intent SET status=? WHERE id=?",
                   (h['status'], h['dispatch_id']))
        db.execute('INSERT INTO position_log(asset_id,at,location,holder,reason,handover_id,'
                   'source_id) VALUES(?,?,?,?,?,?,?)',
                   (h['asset_id'], h['at'], 'external', h['actual_custodian'],
                    h['status'], h['id'], h['source_id']))
        _open_dispute(db, h['loan_id'], h['id'], h['status'], h['at'],
                      h['reason'] or h['status'])
    else:
        db.execute("UPDATE dispatch_intent SET status='held' WHERE id=?",
                   (h['dispatch_id'],))
        vault.hold_at_shop(db, h['asset_id'], h['at'], h['id'], h['source_id'])
        db.execute('INSERT INTO handoff_event(plan_id,phase,state,at,detail,source_id) '
                   'VALUES(?,?,?,?,?,?)',
                   (h['plan_id'], 'handover', 'held', h['at'],
                    json.dumps({'holder': 'shop-vault', 'reason': h['reason']},
                               ensure_ascii=False), h['source_id']))


def _maybe_redeem(db, loan_id):
    """A lawful, countersigned delivery closes the redemption even when the old
    ``decision`` counter step was never run."""
    db.execute("UPDATE loan SET status='redeemed' WHERE id=? AND status NOT IN "
               "('redeemed','forfeited')", (loan_id,))


# ---------------------------------------------------------------------------
# 恢复协议 / 对账
# ---------------------------------------------------------------------------

def _open_dispute(db, loan_id, handover_id, reason, moment, detail):
    if db.execute('SELECT 1 FROM dispute WHERE loan_id=? AND handover_id=? AND reason=?',
                  (loan_id, handover_id or '', reason)).fetchone():
        return
    cur = db.execute('INSERT INTO dispute(loan_id,handover_id,reason,opened_at,status,detail) '
                     'VALUES(?,?,?,?,?,?)', (loan_id, handover_id or '', reason, moment,
                                             'open', detail))
    db.execute('INSERT INTO recourse(dispute_id,action,against,opened_at,status,detail) '
               'VALUES(?,?,?,?,?,?)',
               (cur.lastrowid, 'locate_and_recover_asset',
                detail.split(':')[-1] if ':' in detail else 'holder_unknown', moment,
                'open', 'asset_position_must_be_reconciled'))


def _reconcile(db):
    """重放物理事实，补齐数据库裁决；只追加，不删除历史。"""
    # 0) 回执已存在但落库中断：按回执行重放终态（反向裂缝）。
    for h in [dict(x) for x in db.execute(
            'SELECT * FROM physical_handover WHERE finalized=0 ORDER BY at,id')]:
        _finalize(db, h['id'])

    # 1) 仅有写前意图、或已抬出但无回执的放行。
    for it in [dict(x) for x in db.execute('SELECT * FROM dispatch_intent ORDER BY at,id')]:
        receipts = db.execute('SELECT * FROM physical_handover WHERE dispatch_id=?',
                              (it['id'],)).fetchall()
        asset = db.execute('SELECT status FROM asset WHERE asset_id=?',
                           (it['asset_id'],)).fetchone()
        if it['status'] == 'intent':
            if not receipts:
                if asset and asset['status'] == 'sealed' and db.execute(
                        'SELECT 1 FROM vault_slot WHERE occupied_asset=?',
                        (it['asset_id'],)).fetchone():
                    # 抬出尚未发生：安全回退，绝不留一个假的“在途”。
                    db.execute("UPDATE dispatch_intent SET status='aborted_before_lift' "
                               "WHERE id=?", (it['id'],))
                    db.execute('INSERT INTO handoff_event(plan_id,phase,state,at,detail,source_id) '
                               'VALUES(?,?,?,?,?,?)',
                               (it['plan_id'], 'recovery', 'aborted_before_lift', it['at'],
                                'no_physical_receipt_and_slot_still_occupied', it['source_id']))
                else:
                    db.execute("UPDATE dispatch_intent SET status='physical_outcome_unknown' "
                               "WHERE id=?", (it['id'],))
                    vault.hold_at_shop(db, it['asset_id'], it['at'], it['id'], it['source_id'])
                    _open_dispute(db, it['loan_id'], '', 'physical_outcome_unknown', it['at'],
                                  'dispatch_intent_without_receipt:shop-vault')
            continue
        if it['status'] == 'lifted' and not receipts:
            # 已抬出但尚无收件回执：明确责任在门店库房，不能报告客户已领取。
            vault.hold_at_shop(db, it['asset_id'], it['at'], it['id'], it['source_id'])
            db.execute("UPDATE dispatch_intent SET status='awaiting_receipt' WHERE id=?",
                       (it['id'],))

    # 2) 已落库的物理回执：按“今天已知的全部证据”回溯判断合法性。
    for h in [dict(x) for x in db.execute(
            'SELECT * FROM physical_handover WHERE accepted=1 ORDER BY at,id')]:
        if h['status'] not in ('delivered', 'unauthorized_recipient',
                               'contested_late_revocation'):
            continue
        late = [r for r in _late_revocations_for(db, h['recipient'], h['at'])]
        if h['route'] == 'direct_heir':
            retro = claim_status(db, h['loan_id'], h['at'], evidence_cutoff=None)
            if late:
                if h['status'] != 'contested_late_revocation':
                    db.execute("UPDATE physical_handover SET status='contested_late_revocation', "
                               "reason='revocation_effective_before_delivery' WHERE id=?",
                               (h['id'],))
                _open_dispute(db, h['loan_id'], h['id'], 'late_revocation', h['at'],
                              'revocation_effective_before_delivery:' + h['recipient'])
                db.execute("UPDATE loan SET status='disputed' WHERE id=? AND status!='disputed'",
                           (h['loan_id'],))
            elif h['recipient'] not in retro['eligible'] and \
                    h['status'] != 'unauthorized_recipient':
                db.execute("UPDATE physical_handover SET status='unauthorized_recipient', "
                           "reason='recipient_not_authorized' WHERE id=?", (h['id'],))
                _open_dispute(db, h['loan_id'], h['id'], 'wrong_recipient', h['at'],
                              'recipient_not_authorized:' + h['recipient'])
        else:
            court = neutral_custody_status(db, h['loan_id'], h['at'], evidence_cutoff=None)
            point_ok = court['status'] == 'permitted' and h['recipient'] == court['custody_point']
            if not point_ok:
                _open_dispute(db, h['loan_id'], h['id'], 'custody_point_not_permitted',
                              h['at'], 'custody_point_mismatch:' + h['recipient'])
            elif not h['court_registration_id'] or db.execute(
                    'SELECT 1 FROM counter_verification WHERE id=? AND method=? AND matched=1',
                    (h['court_registration_id'], 'court_receipt')).fetchone() is None:
                _open_dispute(db, h['loan_id'], h['id'], 'court_receipt_missing',
                              h['at'], 'custody_point_receipt_missing:' + h['recipient'])

    # 3) 旧裂缝：资产已 released，却没有任何有效回执。
    for a in [dict(x) for x in db.execute("SELECT * FROM asset WHERE status='released'")]:
        loan = db.execute('SELECT * FROM loan WHERE asset_id=? ORDER BY rowid DESC LIMIT 1',
                          (a['asset_id'],)).fetchone()
        if not loan:
            continue
        accepted = db.execute("SELECT 1 FROM physical_handover WHERE asset_id=? AND accepted=1",
                              (a['asset_id'],)).fetchone()
        if not accepted:
            _open_dispute(db, loan['id'], '', 'released_without_receipt', loan['due_at'],
                          'asset_released_without_physical_receipt:shop-vault')
    db.commit()


def reconcile(path):
    with open_db(path) as db, db:
        _reconcile(db)
        return {'reconcile': 'ok'}


# ---------------------------------------------------------------------------
# 状态汇总：债务 / 合法领取权 / 实际交付 三条线分开
# ---------------------------------------------------------------------------

def _latest_moment(db, loan):
    moments = []
    for table, col in (('payment', 'at'), ('decision', 'at'), ('handoff_plan', 'fixed_at'),
                       ('physical_handover', 'at'), ('dispatch_intent', 'at')):
        row = db.execute(f'SELECT MAX({col}) FROM {table}' +
                         (' WHERE loan_id=?' if table not in ('handoff_plan',) else ' WHERE loan_id=?'),
                         (loan['id'],)).fetchone()
        if row and row[0]:
            moments.append(row[0])
    for r in db.execute('SELECT MAX(received_at),MAX(effective_from) FROM inheritance_document'):
        moments.extend(x for x in r if x)
    return max(moments) if moments else loan['due_at']


def delivery_state(db, loan):
    rows = [dict(x) for x in db.execute(
        'SELECT * FROM physical_handover WHERE loan_id=? ORDER BY at,id', (loan['id'],))]
    accepted = [r for r in rows if r['accepted']]
    if accepted:
        h = accepted[-1]
        lawful = h['status'] == 'delivered'
        return {'status': h['status'], 'lawful': lawful, 'recipient': h['recipient'],
                'custodian': h['actual_custodian'], 'at': h['at'], 'handover_id': h['id'],
                'route': h['route']}
    if rows:
        h = rows[-1]
        return {'status': 'held_at_shop', 'lawful': False, 'recipient': None,
                'custodian': 'shop-vault', 'at': h['at'], 'handover_id': h['id'],
                'route': h['route'], 'attempted_recipient': h['recipient']}
    intent = db.execute('SELECT * FROM dispatch_intent WHERE loan_id=? ORDER BY at,id DESC LIMIT 1',
                        (loan['id'],)).fetchone()
    asset = db.execute('SELECT status FROM asset WHERE asset_id=?', (loan['asset_id'],)).fetchone()
    if intent:
        if intent['status'] == 'aborted_before_lift':
            # 崩溃恢复确认抬出从未发生：交付尚未开始，物仍在原库位。
            return {'status': 'not_started', 'lawful': False, 'recipient': None,
                    'custodian': 'shop-vault', 'at': intent['at'], 'handover_id': None,
                    'route': intent['route']}
        return {'status': intent['status'], 'lawful': False, 'recipient': None,
                'custodian': 'shop-vault', 'at': intent['at'], 'handover_id': None,
                'route': intent['route']}
    if asset and asset['status'] == 'released':
        return {'status': 'released_without_receipt', 'lawful': False, 'recipient': None,
                'custodian': 'shop-vault', 'at': loan['due_at'], 'handover_id': None,
                'route': None}
    return {'status': 'not_started', 'lawful': False, 'recipient': None, 'custodian': None,
            'at': None, 'handover_id': None, 'route': None}


def loan_status(db, loan):
    if not hasattr(db, 'execute'):
        with open_db(db) as con:
            return loan_status(con, loan)
    paid = db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payment WHERE loan_id=?',
                      (loan['id'],)).fetchone()[0]
    outstanding = loan['principal_cents'] + loan['interest_cents'] - paid
    as_of = _latest_moment(db, loan)
    claim = claim_status(db, loan['id'], as_of, evidence_cutoff=as_of)
    delivery = delivery_state(db, loan)
    plan = fixed_plan(db, loan['id'])
    disputes = [dict(x) for x in db.execute(
        'SELECT id,handover_id,reason,opened_at,status FROM dispute WHERE loan_id=? ORDER BY id',
        (loan['id'],))]
    recourses = [dict(x) for x in db.execute(
        'SELECT r.* FROM recourse r JOIN dispute d ON d.id=r.dispute_id WHERE d.loan_id=? ORDER BY r.id',
        (loan['id'],))]
    return {'loan_id': loan['id'],
            'debt': {'settled': outstanding == 0, 'outstanding_cents': outstanding,
                     'payments': db.execute('SELECT COUNT(*) FROM payment WHERE loan_id=?',
                                            (loan['id'],)).fetchone()[0],
                     'payers': [dict(x) for x in db.execute(
                         'SELECT payer,relationship FROM payment_payer WHERE loan_id=? ORDER BY payment_id',
                         (loan['id'],))]},
            'claim': {'status': claim['status'], 'eligible': claim['eligible'],
                      'court': claim['forced_court_order'] or (claim['court_orders'] or []),
                      'as_of': as_of},
            'delivery': delivery,
            'route': plan['route'] if plan else None,
            'disputes': disputes, 'recourses': recourses}
