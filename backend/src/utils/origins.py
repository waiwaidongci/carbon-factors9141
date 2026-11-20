"""本站记录的原号分配。

本站记录的原号默认等于自增编号；只有在同站原号空间被历史导入占用时，
才顺延到未被占用的值，既保持“本站新编号”可读，又避免唯一约束冲突。
"""
from sqlalchemy import func

def local_origin_id(db,model,station,tentative):
 taken=db.query(model.id).filter(model.station==station,model.origin_id==tentative).first()
 if not taken: return tentative
 highest=db.query(func.max(model.origin_id)).filter(model.station==station).scalar() or 0
 return max(highest,tentative)+1
