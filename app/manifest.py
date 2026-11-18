import hashlib, time
from app.config import RULE_VERSION
def make(text, recipe=None):
    """运行清单：文件摘要 + 规则版本 + 配方口径共同确定批次标识。"""
    from app.recipe import DEFAULT_RECIPE, digest as recipe_digest, normalize_version
    recipe=normalize_version(recipe) if recipe is not None else DEFAULT_RECIPE
    digest=hashlib.sha256(text.encode('utf-8')).hexdigest()
    identity=RULE_VERSION+':'+recipe+':'+recipe_digest(recipe)+':'+digest
    return {'run_id':hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16], 'sha256':digest, 'rule_version':RULE_VERSION, 'recipe_version':recipe, 'recipe_digest':recipe_digest(recipe), 'parameters':{}, 'created_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
