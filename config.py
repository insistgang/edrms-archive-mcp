import os
from typing import Dict, List, Any

# 服务基础配置
SERVER_HOST: str = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT: int = int(os.getenv("SERVER_PORT", "8000"))

# 鸿翼 EDRMS 后端配置
# 现场若部署了 EDRMS，填入内部地址例如: http://10.20.10.88:8080
EDRMS_BASE_URL: str = os.getenv("EDRMS_BASE_URL", "").rstrip("/")
EDRMS_TIMEOUT_SECONDS: float = float(os.getenv("EDRMS_TIMEOUT_SECONDS", "8.0"))

# AUTO 在未配置地址时使用 Mock；配置地址后出错不会回退。MOCK 是演示模式，LIVE 必须配置实机。
RUN_MODE: str = os.getenv("RUN_MODE", "LIVE").upper()

# 客户 LLM 接口配置（如无，系统将采用基于规则的精准摘要引擎生成 msg）
LLM_API_BASE: str = os.getenv("LLM_API_BASE", "")
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "qwen-max")
LLM_GENERATE_MSG: bool = os.getenv("LLM_GENERATE_MSG", "false").lower() == "true"

# 权限与身份模拟字典（用于单点登录/Token校验与权限前置过滤模拟）
# 真实生产环境下可由 /edrms/api/permCalc/buildQueryByES 动态下发
MOCK_USER_PERMISSIONS: Dict[str, Dict[str, Any]] = {
    "token_normal_user": {
        "userId": "U1001",
        "userName": "普通员工张三",
        "dept": "零售业务部",
        "roles": ["ROLE_PUBLIC", "DEPT_ALL", "SEC_INTERNAL"]
    },
    "token_compliance_officer": {
        "userId": "U2002",
        "userName": "合规专员李四",
        "dept": "法律合规部",
        "roles": ["ROLE_PUBLIC", "DEPT_ALL", "SEC_INTERNAL", "ROLE_COMPLIANCE", "SEC_SECRET"]
    },
    "token_risk_director": {
        "userId": "U3003",
        "userName": "风控总监王五",
        "dept": "风险管理部",
        "roles": ["ROLE_PUBLIC", "DEPT_ALL", "SEC_INTERNAL", "ROLE_RISK_MGT", "SEC_CONFIDENTIAL", "ROLE_COMPLIANCE", "SEC_SECRET"]
    }
}
