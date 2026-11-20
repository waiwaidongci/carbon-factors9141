import hashlib,json
def snapshot_checksum(snapshot):
 """与提交报告时完全一致的冻结底稿校验和。"""
 return hashlib.sha256(json.dumps(snapshot,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
