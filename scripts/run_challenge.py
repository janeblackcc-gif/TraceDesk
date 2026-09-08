"""Exploratory challenge suite authored after the easy regression, never advertised as blind."""
import json
import sys
import tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.service import Service
ROWS = [
 {'question':'凭据作废前能用几刻钟？','kind':'paraphrase','gold':'访问令牌有效期为 15 分钟'},
 {'question':'不认识的程序占着服务地址，直接关掉它合适吗？','kind':'paraphrase','gold':'不要直接关闭未知进程'},
 {'question':'出了事故怎样还原到先前的资料状态？','kind':'paraphrase','gold':'备份前停止写入'},
 {'question':'相同资料反复拖进来，会越存越多吗？','kind':'paraphrase','gold':'视为重复上传，不新增分块'},
 {'question':'扫描件能靠重新做索引解决吗？','kind':'paraphrase','gold':'直接重建向量索引不能解决'},
 {'question':'项目交接时我该从哪种访问级别开始？','kind':'paraphrase','gold':'新人向项目维护人申请 reader 权限'},
 {'question':'E041 应该联系哪位管理员，电话号码是多少？','kind':'hard_negative','gold':None},
 {'question':'访问令牌采用 RSA 还是 HMAC 签名？','kind':'hard_negative','gold':None},
 {'question':'运行日志备份存放在哪个具体云存储桶？','kind':'hard_negative','gold':None},
 {'question':'Python 服务能支持多少并发用户？','kind':'hard_negative','gold':None},
 {'question':'部署到 GPU 集群时显存上限是多少？','kind':'hard_negative','gold':None},
 {'question':'上传文件的商业授权费用是多少？','kind':'hard_negative','gold':None},
]
if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as tmp:
        s = Service(Path(tmp) / 'challenge.db'); s.load_demo()
        for row in ROWS:
            result = s.ask(row['question'], 'Atlas 演示项目', 'v2')
            row.update(status=result['status'], retrieved=[c['filename'] + ':' + c['heading'] for c in result['sources']])
            row['passed'] = result['status'] == 'no_evidence' if row['kind'] == 'hard_negative' else any(row['gold'] in c['text'] for c in result['sources'])
        s.store.close()
    report = {'notice':'探索性困难集，在简易基线运行后编写，非独立盲测；证据模式返回相关摘录不等于回答到了问题。此处的失败是覆盖/拒答失败，不能直接等同于模型幻觉。',
              'profile':'evidence','method':'hybrid','total':len(ROWS),'passed':sum(r['passed'] for r in ROWS),'rows':ROWS}
    for kind in ('paraphrase','hard_negative'):
        chosen = [r for r in ROWS if r['kind'] == kind]; report[kind] = {'passed':sum(r['passed'] for r in chosen),'total':len(chosen)}
    (ROOT / 'eval/challenge_questions.json').write_text(json.dumps(ROWS,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROOT / 'evidence/challenge_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
