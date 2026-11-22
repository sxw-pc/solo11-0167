import json
import pytest

from pawnproof import asset, loan, vault
from pawnproof import redemption_handoff as rh
from pawnproof.report import audit, report
from pawnproof.store import open_db

FIX = __import__('pathlib').Path(__file__).parent.parent / 'fixtures'


def fixture(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    return p


def base(tmp_path):
    """C1 的 L1 已足额还款，G1 仍在 V1 封存。"""
    db = tmp_path / 'state.sqlite'
    for func, name in [(asset.import_asset, 'asset-ok.json'),
                       (asset.ownership, 'ownership-ok.json'),
                       (asset.appraise, 'appraisal-ok.json'),
                       (vault.import_vault, 'vault-ok.json'),
                       (vault.custody, 'custody-ok.json'),
                       (loan.originate, 'loan-ok.json'),
                       (loan.pay, 'payment-ok.json')]:
        func(db, FIX / name)
    return db


def deceased_estate(tmp_path, *, with_authorization=True, with_court=True):
    db = base(tmp_path)
    rh.customer_status(db, FIX / 'customer-deceased.json')
    rh.heir_share(db, FIX / 'heirs-c1.json')
    if with_authorization:
        rh.document(db, FIX / 'auth-h2-to-h1.json')
    if with_court:
        rh.document(db, FIX / 'court-voluntary-custody.json')
        rh.custody_point(db, FIX / 'custody-point-court.json')
    rh.counter_check(db, FIX / 'counter-h1.json')
    return db


def rows(db, table):
    with open_db(db) as con:
        return [dict(x) for x in con.execute(f'SELECT * FROM {table}')]


def asset_status(db):
    with open_db(db) as con:
        return con.execute("SELECT status FROM asset WHERE asset_id='G1'").fetchone()[0]


def slot_holder(db):
    with open_db(db) as con:
        return con.execute("SELECT occupied_asset FROM vault_slot WHERE slot_id='V1'").fetchone()[0]


# ---------------------------------------------------------------------------
# 1. 付款人 ≠ 领取权：债务清偿与合法领取权分离
# ---------------------------------------------------------------------------

def test_heir_may_settle_debt_but_payment_grants_no_pickup_right(tmp_path):
    db = base(tmp_path)
    # 借款人死亡后，本人不能作为付款人；由第三人垫付。
    dead_payer = fixture(tmp_path, 'dead-payer.json', {
        'id': 'P-DEAD', 'loan_id': 'L1', 'amount': '1.00',
        'at': '2026-03-20T10:00:00Z', 'payer': 'C1'})
    rh.customer_status(db, FIX / 'customer-deceased.json')
    with pytest.raises(ValueError, match='payer_must_be_living_or_representative'):
        loan.pay(db, dead_payer)

    db2 = base(tmp_path / 'second')
    rh.customer_status(db2, FIX / 'customer-deceased.json')
    rh.heir_share(db2, FIX / 'heirs-c1.json')
    # 全额还款已由继承人垫付（fixture payment-ok 已导入），但债务清偿不等于领取权。
    claim = rh.loan_status(db2, rows(db2, 'loan')[0])
    assert claim['debt']['settled'] is True
    # H2 尚未授权 H1 的卷宗（只导入份额）— 任何人都不能领取。
    assert claim['claim']['status'] == 'no_authorized_representative'
    assert claim['delivery']['status'] == 'not_started'


def test_late_revocation_after_prior_authorization_removes_authority(tmp_path):
    db = deceased_estate(tmp_path)
    status = rh.claim_status(db, 'L1', '2026-03-30T00:00:00Z', evidence_cutoff='2026-03-30T00:00:00Z')
    assert status['status'] == 'eligible' and status['eligible'] == ['H1']
    # 撤回在 11:00 生效、12:00 才送达：交付时点 11:30 的事后回溯不得默认合法。
    rh.document(db, FIX / 'revocation-h2-late.json')
    retro = rh.claim_status(db, 'L1', '2026-03-31T11:30:00Z', evidence_cutoff=None)
    assert 'H1' not in retro['eligible']


def test_metadata_only_document_is_not_authorization(tmp_path):
    db = deceased_estate(tmp_path, with_authorization=False)
    meta = FIX / 'auth-dir-metadata-only.json'
    rh.document(db, meta)
    with open_db(db) as con:
        doc = [dict(x) for x in con.execute(
            "SELECT * FROM inheritance_document WHERE id='D-AUTH-META'")][0]
    assert doc['state'] == 'metadata_only' and not doc['body_present']
    status = rh.claim_status(db, 'L1', '2026-03-31T11:30:00Z',
                             evidence_cutoff='2026-03-31T11:30:00Z')
    assert status['eligible'] == [] and status['status'] == 'no_authorized_representative'
    # 正文后到后才成为完整授权。
    rh.document_body(db, FIX / 'auth-dir-body.json')
    assert rh.claim_status(db, 'L1', '2026-03-31T11:30:00Z',
                           evidence_cutoff='2026-03-31T11:30:00Z')['eligible'] == ['H1']


def test_out_of_order_imports_are_replayed_by_evidence_time(tmp_path):
    db = base(tmp_path)
    # 先到法院文件与撤回，再补身份、份额与授权（乱序）。
    rh.document(db, FIX / 'court-voluntary-custody.json')
    rh.custody_point(db, FIX / 'custody-point-court.json')
    rh.customer_status(db, FIX / 'customer-deceased.json')
    rh.document(db, FIX / 'auth-h2-to-h1.json')
    rh.heir_share(db, FIX / 'heirs-c1.json')
    status = rh.claim_status(db, 'L1', '2026-03-31T11:30:00Z',
                             evidence_cutoff='2026-03-31T11:30:00Z')
    assert status['status'] == 'eligible' and status['eligible'] == ['H1']


# ---------------------------------------------------------------------------
# 2. 路线固定与两条合法路线
# ---------------------------------------------------------------------------

def test_route_must_be_fixed_before_movement_and_cannot_change(tmp_path):
    db = deceased_estate(tmp_path)
    with pytest.raises(ValueError, match='recipient'):
        # 计划的收件人不是有权人。
        rh.fix_route(db, fixture(tmp_path, 'bad-route.json', {
            'id': 'R-BAD', 'loan_id': 'L1', 'route': 'direct_heir', 'recipient': 'H2',
            'recipient_kind': 'heir', 'slot_id': 'V1', 'at': '2026-03-31T11:20:00Z'}))
    rh.fix_route(db, FIX / 'route-direct-h1.json')
    with pytest.raises(ValueError, match='route_already_fixed'):
        rh.fix_route(db, FIX / 'route-neutral-court.json')
    assert slot_holder(db) == 'G1'  # 固定路线本身不动物。


def test_direct_heir_route_delivers_to_h1_and_only_then_releases(tmp_path):
    db = deceased_estate(tmp_path)
    rh.fix_route(db, FIX / 'route-direct-h1.json')
    rh.dispatch(db, FIX / 'dispatch-h1.json')
    # 已抬出库位、尚无外部回执：库位空，但绝不能报告“已领取”。
    assert slot_holder(db) is None
    assert asset_status(db) == 'held_at_shop'
    interim = rh.loan_status(db, rows(db, 'loan')[0])
    assert interim['delivery']['status'] == 'awaiting_receipt'
    assert interim['delivery']['custodian'] == 'shop-vault'

    result = rh.confirm_handover(db, FIX / 'handover-h1.json')
    assert result['status'] == 'delivered' and result['actual_custodian'] == 'H1'
    assert asset_status(db) == 'released'
    final = rh.loan_status(db, rows(db, 'loan')[0])
    assert final['debt']['settled'] and final['claim']['status'] == 'eligible'
    assert final['delivery']['lawful'] and final['delivery']['custodian'] == 'H1'
    assert final['route'] == 'direct_heir'
    assert audit(db)['replay_ok']


def test_neutral_custody_route_never_reports_h1_collected(tmp_path):
    db = deceased_estate(tmp_path)
    rh.counter_check(db, FIX / 'counter-court.json')
    rh.fix_route(db, FIX / 'route-neutral-court.json')
    rh.dispatch(db, FIX / 'dispatch-court.json')
    result = rh.confirm_handover(db, FIX / 'handover-court.json')
    assert result['actual_custodian'] == 'V-COURT'
    status = rh.loan_status(db, rows(db, 'loan')[0])
    assert status['delivery']['custodian'] == 'V-COURT'
    assert status['delivery']['recipient'] == 'V-COURT'
    assert 'H1' not in json.dumps(status['delivery'], ensure_ascii=False)
    assert asset_status(db) == 'released'


def test_same_estate_two_routes_land_with_different_custodians(tmp_path):
    direct = deceased_estate(tmp_path / 'direct')
    rh.fix_route(direct, FIX / 'route-direct-h1.json')
    rh.dispatch(direct, FIX / 'dispatch-h1.json')
    rh.confirm_handover(direct, FIX / 'handover-h1.json')

    neutral = deceased_estate(tmp_path / 'neutral')
    rh.counter_check(neutral, FIX / 'counter-court.json')
    rh.fix_route(neutral, FIX / 'route-neutral-court.json')
    rh.dispatch(neutral, FIX / 'dispatch-court.json')
    rh.confirm_handover(neutral, FIX / 'handover-court.json')

    a = rh.loan_status(direct, rows(direct, 'loan')[0])
    b = rh.loan_status(neutral, rows(neutral, 'loan')[0])
    assert a['debt']['settled'] and b['debt']['settled']
    assert a['delivery']['custodian'] == 'H1'
    assert b['delivery']['custodian'] == 'V-COURT'


# ---------------------------------------------------------------------------
# 3. 同一故障轨迹下的两种相反裂缝
# ---------------------------------------------------------------------------

def test_legacy_release_then_external_refusal_leaves_no_custodian(tmp_path):
    """原 loan.decide 路径：库位清空并 released，但外部收件失败也无从恢复。"""
    db = base(tmp_path)
    assert loan.decide(db, FIX / 'decision-ok.json')['status'] == 'approved'
    assert slot_holder(db) is None and asset_status(db) == 'released'
    # 货物实际仍在门店/被骑手退回，但账上已经“客户已领取”。
    status = rh.loan_status(db, rows(db, 'loan')[0])
    assert status['delivery']['status'] == 'released_without_receipt'
    assert status['delivery']['custodian'] == 'shop-vault'
    # 对账后开出争议与追偿线索，旧事实不删除。
    rh.reconcile(db)
    assert [d['reason'] for d in rh.loan_status(db, rows(db, 'loan')[0])['disputes']] == \
        ['released_without_receipt']
    assert asset_status(db) == 'released'  # 历史事实保留


def test_db_commit_then_physical_refusal_is_recoverable(tmp_path):
    """新协议：事务提交后仍可能收件失败；失败回执保留实际保管方。"""
    db = deceased_estate(tmp_path)
    rh.fix_route(db, FIX / 'route-direct-h1.json')
    rh.dispatch(db, FIX / 'dispatch-h1.json')
    refused = fixture(tmp_path, 'h1-refused.json', {
        'id': 'PH-REFUSED', 'dispatch_id': 'X-H1', 'recipient': 'H1', 'accepted': False,
        'reason': 'recipient_refused_at_door',
        'actor_a': 'guard-a', 'actor_b': 'guard-b',
        'counter_verification_id': 'CV-H1', 'receipt_sha': '9' * 64,
        'at': '2026-03-31T11:30:00Z'})
    rh.counter_check(db, FIX / 'counter-h1.json')
    rh.confirm_handover(db, refused)
    assert asset_status(db) == 'held_at_shop'
    status = rh.loan_status(db, rows(db, 'loan')[0])
    assert status['delivery']['status'] == 'held_at_shop'
    assert status['delivery']['lawful'] is False
    assert status['debt']['settled'] is True
    # 物仍可定位：位置账最后一次记录是门店库房。
    pos = rows(db, 'position_log')[-1]
    assert pos['holder'] == 'shop-vault' and pos['location'] == 'shop-vault'


def test_receipt_saved_but_db_finalize_lost_reconciles_from_receipt(tmp_path):
    """回执文件已经存在但数据库落库失败的相反裂缝：恢复协议只回放回执。"""
    db = deceased_estate(tmp_path)
    rh.counter_check(db, FIX / 'counter-h1.json')
    rh.fix_route(db, FIX / 'route-direct-h1.json')
    rh.dispatch(db, FIX / 'dispatch-h1.json')
    rh.confirm_handover(db, FIX / 'handover-h1.json', finalize=False)
    assert asset_status(db) == 'held_at_shop'
    assert rows(db, 'physical_handover')[0]['finalized'] == 0
    rh.reconcile(db)
    assert asset_status(db) == 'released'
    assert rows(db, 'physical_handover')[0]['finalized'] == 1
    assert rows(db, 'loan')[0]['status'] == 'redeemed'


def test_crash_after_intent_before_lift_rolls_back_to_sealed(tmp_path):
    db = deceased_estate(tmp_path)
    rh.fix_route(db, FIX / 'route-direct-h1.json')
    rh.dispatch(db, FIX / 'dispatch-h1.json', simulate='crash_after_intent')
    assert asset_status(db) == 'sealed' and slot_holder(db) == 'G1'
    rh.reconcile(db)
    assert rows(db, 'dispatch_intent')[0]['status'] == 'aborted_before_lift'
    assert asset_status(db) == 'sealed' and slot_holder(db) == 'G1'
    assert rh.loan_status(db, rows(db, 'loan')[0])['delivery']['status'] == 'not_started'


# ---------------------------------------------------------------------------
# 4. 晚到撤回、错人签收、事后争议与追偿
# ---------------------------------------------------------------------------

def test_contested_late_revocation_preserves_facts_and_opens_recourse(tmp_path):
    db = deceased_estate(tmp_path)
    rh.counter_check(db, FIX / 'counter-h1.json')
    rh.fix_route(db, FIX / 'route-direct-h1.json')
    rh.dispatch(db, FIX / 'dispatch-h1.json')
    rh.confirm_handover(db, FIX / 'handover-h1.json')
    assert rows(db, 'loan')[0]['status'] == 'redeemed'
    # 12:00 送达、11:00 已生效的撤回：11:30 的交付不能默认为合法。
    rh.document(db, FIX / 'revocation-h2-late.json')
    rh.reconcile(db)
    h = rows(db, 'physical_handover')[0]
    assert h['status'] == 'contested_late_revocation'
    assert h['accepted'] == 1  # 物理事实保留
    status = rh.loan_status(db, rows(db, 'loan')[0])
    assert status['delivery']['lawful'] is False
    assert any(d['reason'] == 'late_revocation' for d in status['disputes'])
    assert any(r['action'] == 'locate_and_recover_asset' for r in status['recourses'])
    assert rows(db, 'loan')[0]['status'] == 'disputed'
    # 旧事实不删除：首次合法判断与新判断都在库里。
    assert len(rows(db, 'physical_handover')) == 1
    assert rows(db, 'payment')[0]['amount_cents'] == 52500


def test_wrong_person_signature_is_unauthorized_and_recoverable(tmp_path):
    db = deceased_estate(tmp_path)
    rh.counter_check(db, FIX / 'counter-h2.json')
    # 柜台核过 H2 身份，但 H2 没有单独领取权（H1 才有授权）。
    with pytest.raises(ValueError, match='recipient_not_authorized_at_route_fix'):
        rh.fix_route(db, fixture(tmp_path, 'route-h2.json', {
            'id': 'R-H2', 'loan_id': 'L1', 'route': 'direct_heir', 'recipient': 'H2',
            'recipient_kind': 'heir', 'slot_id': 'V1', 'at': '2026-03-31T11:20:00Z'}))
    # 数据库落库回执时若收件人不是有权人，也标为 unauthorized，而非 delivered。
    rh.fix_route(db, FIX / 'route-direct-h1.json')
    rh.dispatch(db, FIX / 'dispatch-h1.json')
    wrong = fixture(tmp_path, 'wrong.json', {
        'id': 'PH-WRONG', 'dispatch_id': 'X-H1', 'recipient': 'H2', 'accepted': True,
        'actor_a': 'guard-a', 'actor_b': 'guard-b',
        'counter_verification_id': 'CV-H2', 'receipt_sha': '8' * 64,
        'at': '2026-03-31T11:30:00Z'})
    with pytest.raises(ValueError, match='recipient_mismatch'):
        rh.confirm_handover(db, wrong)
    assert asset_status(db) == 'held_at_shop'


def test_court_impound_overrides_private_pickup(tmp_path):
    db = deceased_estate(tmp_path)
    rh.document(db, FIX / 'court-impound.json')  # 10:30 收到，10:00 生效
    status = rh.claim_status(db, 'L1', '2026-03-31T11:30:00Z', evidence_cutoff=None)
    assert status['status'] == 'court_custody_required'
    assert status['custody_recipient'] == 'V-COURT'
    with pytest.raises(ValueError, match='court_priority_blocks_direct_pickup'):
        rh.fix_route(db, FIX / 'route-direct-h1.json')
    # 中立保管路线仍可走，但交付对象必须是法院保管点。
    rh.counter_check(db, FIX / 'counter-court.json')
    rh.fix_route(db, FIX / 'route-neutral-court.json')
    assert asset_status(db) == 'sealed'


# ---------------------------------------------------------------------------
# 5. 三条状态线与重导/审计
# ---------------------------------------------------------------------------

def test_report_and_audit_show_three_separate_lines(tmp_path):
    db = deceased_estate(tmp_path)
    rh.counter_check(db, FIX / 'counter-h1.json')
    rh.fix_route(db, FIX / 'route-direct-h1.json')
    rh.dispatch(db, FIX / 'dispatch-h1.json')
    line = report(db)['three_line_status'][0]
    assert set(line) >= {'debt', 'claim', 'delivery'}
    assert line['debt']['settled'] is True
    assert line['claim']['status'] == 'eligible'
    assert line['delivery']['status'] == 'awaiting_receipt'
    # 相同证据重导不产生新事实。
    before = len(rows(db, 'dispatch_intent'))
    assert rh.dispatch(db, FIX / 'dispatch-h1.json')['replayed']
    assert len(rows(db, 'dispatch_intent')) == before
    rh.confirm_handover(db, FIX / 'handover-h1.json')
    assert audit(db)['replay_ok']


def test_same_delivery_fault_two_protocols_give_opposite_results(tmp_path):
    """同一条真实故障轨迹：执法人员到达门店时外部收件人拒绝接收/失联。
    原协议只剩“已清空 + released”，新协议保留门店库房为实际保管方。
    """
    fault = {'at': '2026-03-31T11:30:00Z', 'reason': 'recipient_unreachable'}

    # 原协议：decide(redeem) 的数据库事务先提交，外部收件随后失败。
    legacy = base(tmp_path / 'legacy')
    loan.decide(legacy, FIX / 'decision-ok.json')
    legacy_state = rh.loan_status(legacy, rows(legacy, 'loan')[0])
    assert legacy_state['delivery']['status'] == 'released_without_receipt'
    assert legacy_state['delivery']['custodian'] == 'shop-vault'
    assert asset_status(legacy) == 'released' and slot_holder(legacy) is None

    # 新协议：同一故障点只写一条被拒回执，物明确仍在门店。
    modern = deceased_estate(tmp_path / 'modern')
    rh.fix_route(modern, FIX / 'route-direct-h1.json')
    rh.dispatch(modern, FIX / 'dispatch-h1.json')
    refused = fixture(tmp_path, 'same-fault.json', {
        'id': 'PH-FAULT', 'dispatch_id': 'X-H1', 'recipient': 'H1', 'accepted': False,
        'reason': fault['reason'], 'actor_a': 'guard-a', 'actor_b': 'guard-b',
        'counter_verification_id': 'CV-H1', 'receipt_sha': '4' * 64, 'at': fault['at']})
    rh.confirm_handover(modern, refused)
    modern_state = rh.loan_status(modern, rows(modern, 'loan')[0])
    assert modern_state['delivery']['status'] == 'held_at_shop'
    assert modern_state['delivery']['custodian'] == 'shop-vault'
    assert asset_status(modern) == 'held_at_shop'

    # 两侧债务都为零，但只有新协议保留了可追查的实际保管方。
    assert legacy_state['debt']['settled'] and modern_state['debt']['settled']
    assert legacy_state['delivery']['lawful'] is False
    assert modern_state['delivery']['lawful'] is False


def test_legacy_redeem_decision_is_blocked_once_estate_is_known(tmp_path):
    db = deceased_estate(tmp_path)
    result = loan.decide(db, FIX / 'decision-ok.json')
    assert (result['status'], result['reason']) == ('blocked', 'heir_handoff_required')
    assert asset_status(db) == 'sealed' and slot_holder(db) == 'G1'
    assert rows(db, 'loan')[0]['status'] == 'active'


def test_legacy_redeem_decision_kept_for_living_borrower(tmp_path):
    db = base(tmp_path)
    assert loan.decide(db, FIX / 'decision-ok.json')['status'] == 'approved'
    assert asset_status(db) == 'released' and slot_holder(db) is None
    assert audit(db)['replay_ok']


def test_cli_handoff_entrypoints(tmp_path):
    import os, subprocess, sys
    env = {**os.environ, 'PYTHONPATH': str(__import__('pathlib').Path(__file__).parent.parent)}
    db = str(tmp_path / 'cli.sqlite')
    steps = [
        ('asset-import', 'asset-ok.json'), ('ownership-import', 'ownership-ok.json'),
        ('appraisal-import', 'appraisal-ok.json'), ('vault-import', 'vault-ok.json'),
        ('custody-import', 'custody-ok.json'), ('loan-import', 'loan-ok.json'),
        ('payment-import', 'payment-ok.json'), ('customer-import', 'customer-deceased.json'),
        ('heir-import', 'heirs-c1.json'), ('document-import', 'auth-h2-to-h1.json'),
        ('document-import', 'court-voluntary-custody.json'),
        ('counter-check-import', 'counter-h1.json'),
        ('route-import', 'route-direct-h1.json'), ('dispatch-import', 'dispatch-h1.json'),
        ('handover-import', 'handover-h1.json')]
    for cmd, name in steps:
        p = subprocess.run([sys.executable, '-m', 'pawnproof', cmd, str(FIX / name), '--db', db],
                           env=env, capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
    p = subprocess.run([sys.executable, '-m', 'pawnproof', 'report', '--db', db],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    line = json.loads(p.stdout)['three_line_status'][0]
    assert line['delivery']['custodian'] == 'H1'
    p = subprocess.run([sys.executable, '-m', 'pawnproof', 'audit', '--db', db],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)['replay_ok'] is True


def test_altered_receipt_with_same_id_is_rejected(tmp_path):
    db = deceased_estate(tmp_path)
    rh.counter_check(db, FIX / 'counter-h1.json')
    rh.fix_route(db, FIX / 'route-direct-h1.json')
    rh.dispatch(db, FIX / 'dispatch-h1.json')
    rh.confirm_handover(db, FIX / 'handover-h1.json')
    altered = json.loads((FIX / 'handover-h1.json').read_text())
    altered['detail'] = 'rewritten receipt'
    with pytest.raises(ValueError, match='event_id_conflict'):
        rh.confirm_handover(db, fixture(tmp_path, 'altered.json', altered))
    assert len(rows(db, 'physical_handover')) == 1


def test_authority_lapse_after_route_fix_blocks_lift(tmp_path):
    db = deceased_estate(tmp_path)
    rh.fix_route(db, FIX / 'route-direct-h1.json')
    # 撤回在 11:00 生效并于抬出前送达：库位不得被打开。
    rh.document(db, fixture(tmp_path, 'revoke-in-time.json', {
        'id': 'D-REVOKE-NOW', 'kind': 'revocation', 'customer': 'C1', 'loan_id': 'L1',
        'grantor': 'H2', 'revokes': 'D-AUTH-H1', 'effective_from': '2026-03-31T11:00:00Z',
        'received_at': '2026-03-31T11:20:00Z', 'body_present': True, 'body_sha': '7' * 64}))
    with pytest.raises(ValueError, match='recipient_authority_lapsed_before_dispatch'):
        rh.dispatch(db, FIX / 'dispatch-h1.json')
    assert asset_status(db) == 'sealed' and slot_holder(db) == 'G1'


def test_revocation_of_other_authority_does_not_taint_h1(tmp_path):
    db = deceased_estate(tmp_path)
    # H1 另行委托他人（D-AUTH-OTHER），H2 撤回的是那一份，H1 自领仍有效。
    rh.document(db, fixture(tmp_path, 'auth-other.json', {
        'id': 'D-AUTH-OTHER', 'kind': 'authorization', 'customer': 'C1', 'loan_id': 'L1',
        'grantor': 'H1', 'representative': 'H3', 'scope': 'redeem',
        'effective_from': '2026-03-20T09:00:00Z', 'received_at': '2026-03-21T09:00:00Z',
        'body_present': True, 'body_sha': '6' * 64}))
    rh.document(db, fixture(tmp_path, 'revoke-other.json', {
        'id': 'D-REVOKE-OTHER', 'kind': 'revocation', 'customer': 'C1', 'loan_id': 'L1',
        'grantor': 'H1', 'revokes': 'D-AUTH-OTHER', 'effective_from': '2026-03-31T11:00:00Z',
        'received_at': '2026-03-31T12:00:00Z', 'body_present': True, 'body_sha': '5' * 64}))
    claim = rh.claim_status(db, 'L1', '2026-03-31T11:30:00Z', evidence_cutoff=None)
    assert 'H1' in claim['eligible'] and 'H3' not in claim['eligible']
    rh.fix_route(db, FIX / 'route-direct-h1.json')
    rh.dispatch(db, FIX / 'dispatch-h1.json')
    rh.confirm_handover(db, FIX / 'handover-h1.json')
    rh.reconcile(db)
    assert rh.loan_status(db, rows(db, 'loan')[0])['delivery']['lawful'] is True


def test_third_party_payer_is_reported_but_has_no_claim(tmp_path):
    db = base(tmp_path)
    rh.customer_status(db, FIX / 'customer-deceased.json')
    rh.heir_share(db, FIX / 'heirs-c1.json')
    rh.document(db, FIX / 'auth-h2-to-h1.json')
    # 之后由 H1 作为继承人垫付剩余利息，付款人记录为 H1 而非 C1。
    with open_db(db) as con, con:
        con.execute("UPDATE loan SET interest_cents=2600 WHERE id='L1'")
    top_up = fixture(tmp_path, 'h1-topup.json', {
        'id': 'P-H1-TOPUP', 'loan_id': 'L1', 'amount': '1.00',
        'at': '2026-03-20T10:00:00Z', 'payer': 'H1', 'relationship': 'heir'})
    loan.pay(db, top_up)
    debt = rh.loan_status(db, rows(db, 'loan')[0])['debt']
    assert debt['settled'] is True
    assert sorted(p['payer'] for p in debt['payers']) == ['C1', 'H1']
    claim = rh.claim_status(db, 'L1', '2026-03-31T11:30:00Z',
                            evidence_cutoff='2026-03-31T11:30:00Z')
    # 领取权来自继承份额与委托，而不是付款事实。
    assert claim['eligible'] == ['H1']
