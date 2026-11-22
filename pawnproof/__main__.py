import argparse,json,tempfile
from pathlib import Path
from . import asset,vault,loan,privacy,disposal
from .report import report,audit

COMMANDS={'asset-import':asset.import_asset,'ownership-import':asset.ownership,
          'appraisal-import':asset.appraise,'vault-import':vault.import_vault,
          'custody-import':vault.custody,'loan-import':loan.originate,
          'payment-import':loan.pay,'extension-import':loan.extend,
          'notice-import':loan.notice,'decision-import':loan.decide}

def main():
    p=argparse.ArgumentParser(description='Offline pledge custody and redemption evidence')
    s=p.add_subparsers(dest='cmd',required=True)
    for name in COMMANDS:
        x=s.add_parser(name);x.add_argument('file');x.add_argument('--db',required=True)
    for name in ('report','audit'):
        x=s.add_parser(name);x.add_argument('--db',required=True)
    d=s.add_parser('demo');d.add_argument('--db')
    pr=s.add_parser('privacy-request');pr.add_argument('file');pr.add_argument('--db',required=True)
    pv=s.add_parser('privacy-review');pv.add_argument('request_id');pv.add_argument('--db',required=True)

    pp=s.add_parser('privacy-plan')
    pp.add_argument('request_id');pp.add_argument('--db',required=True)
    pp.add_argument('--route',required=True,choices=list(disposal.ROUTES))
    pp.add_argument('--purpose');pp.add_argument('--manifest');pp.add_argument('--media-root')
    pe=s.add_parser('privacy-execute')
    pe.add_argument('request_id');pe.add_argument('--db',required=True)
    pe.add_argument('--vault-dir');pe.add_argument('--failpoint')
    pf=s.add_parser('privacy-verify');pf.add_argument('request_id');pf.add_argument('--db',required=True)
    pn=s.add_parser('privacy-event')
    pn.add_argument('request_id');pn.add_argument('--db',required=True)
    pn.add_argument('--kind',required=True,
                    choices=['withdrawal','legal_hold','dispute'])
    pn.add_argument('--at',required=True);pn.add_argument('--actor',default='disposal-desk')
    pn.add_argument('--detail')
    pa=s.add_parser('privacy-copy-attest')
    pa.add_argument('request_id');pa.add_argument('copy_id');pa.add_argument('file')
    pa.add_argument('--db',required=True)
    pg=s.add_parser('privacy-regulator-access')
    pg.add_argument('request_id');pg.add_argument('--db',required=True)
    pg.add_argument('--share-a');pg.add_argument('--share-b')
    pm=s.add_parser('privacy-naive')
    pm.add_argument('customer');pm.add_argument('--db',required=True)
    pm.add_argument('--approach',required=True,
                    choices=['report_mask_only','column_update_only','delete_raw_only'])
    pm.add_argument('--backup')

    args=p.parse_args()
    if args.cmd in COMMANDS:result=COMMANDS[args.cmd](args.db,args.file)
    elif args.cmd=='report':result=report(args.db)
    elif args.cmd=='audit':result=audit(args.db)
    elif args.cmd=='privacy-request':result=privacy.request(args.db,args.file)
    elif args.cmd=='privacy-review':result=privacy.review(args.db,args.request_id)
    elif args.cmd=='privacy-plan':
        result=disposal.plan(args.db,args.request_id,args.route,args.purpose,
                             args.manifest,args.media_root)
    elif args.cmd=='privacy-execute':
        result=disposal.execute(args.db,args.request_id,args.vault_dir,args.failpoint)
    elif args.cmd=='privacy-verify':
        result=disposal.verify(args.db,args.request_id)
    elif args.cmd=='privacy-event':
        result=disposal.record_event(args.db,args.request_id,args.kind,
                                     args.at,args.actor,args.detail)
    elif args.cmd=='privacy-copy-attest':
        result=disposal.attest_unrecoverable(args.db,args.request_id,
                                             args.copy_id,args.file)
    elif args.cmd=='privacy-regulator-access':
        result=disposal.regulator_access(args.db,args.request_id,
                                         args.share_a,args.share_b)
    elif args.cmd=='privacy-naive':
        result=_naive(args)
    else:
        root=Path(__file__).resolve().parent.parent/'fixtures';db=args.db or str(Path(tempfile.mkdtemp())/'demo.sqlite')
        for name,file in [('asset-import','asset-ok.json'),('ownership-import','ownership-ok.json'),
                          ('appraisal-import','appraisal-ok.json'),('vault-import','vault-ok.json'),
                          ('custody-import','custody-ok.json'),('loan-import','loan-ok.json'),
                          ('payment-import','payment-ok.json'),('decision-import','decision-ok.json')]:
            COMMANDS[name](db,root/file)
        result={'db':db,'audit':audit(db),'loan':report(db)['state']['loan']}
    print(json.dumps(result,ensure_ascii=False,indent=2))


def _naive(args):
    from .evidence import derive_pid
    if args.approach=='report_mask_only':
        backups=(args.backup,) if args.backup else ()
        return disposal.demonstrate_report_mask_only(args.db,args.customer,backups)
    clone=disposal.clone_db(args.db,Path(args.db).with_suffix('.naive.sqlite'))
    pid=derive_pid(args.customer,'naive-demo-salt')
    if args.approach=='column_update_only':
        return disposal.demonstrate_update_only(clone,args.customer,pid)
    return disposal.demonstrate_delete_raw_only(clone,args.customer)


if __name__=='__main__':main()
