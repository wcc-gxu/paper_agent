"""期刊/会议分级查询 — CCF + SCI/JCR 统一分级.

数据: CCF推荐目录 + SCI分区映射，合并为统一等级 A+/A/B/C.
"""

import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── CCF 推荐目录 (2022版) 核心CS会议/期刊 ──────────────

_CCF_DATA: dict[str, list[str]] = {
    # A类会议
    "A_conf": [
        "AAAI", "NeurIPS", "ICML", "IJCAI", "CVPR", "ICCV", "ACL", "EMNLP",
        "SIGCOMM", "SIGMOD", "SIGKDD", "SIGIR", "STOC", "FOCS", "OSDI",
        "SOSP", "S&P", "CCS", "USENIX Security", "NDSS", "CRYPTO", "EUROCRYPT",
        "PLDI", "POPL", "ISCA", "MICRO", "HPCA", "SC", "SIGGRAPH", "MOBICOM",
        "SIGMETRICS", "VLDB", "WWW", "ACM MM", "ICSE", "FSE", "ASE",
    ],
    # B类会议
    "B_conf": [
        "ECCV", "ICRA", "IROS", "AAMAS", "UAI", "AISTATS", "NAACL",
        "COLING", "CoNLL", "CIKM", "WSDM", "ICDM", "ECML-PKDD",
        "PODS", "ICDT", "CAV", "LICS", "CSL", "FM", "ICCAD",
        "DAC", "DATE", "RTSS", "RTAS", "IPSN", "SenSys", "INFOCOM",
        "ACSAC", "ESORICS", "RAID", "PETS", "CHES", "FSE", "ICNP",
        "ICDCS", "Middleware", "CGO", "PACT", "ICS", "EMSOFT",
    ],
    # C类会议
    "C_conf": [
        "ICPR", "ICDAR", "BMVC", "ACCV", "FG", "ICASSP", "INTERSPEECH",
        "EACL", "WMT", "AMIA", "BIBM", "RECOMB", "ISMB", "ICDM",
        "SDM", "PAKDD", "APWeb-WAIM", "WISE", "MDM", "SSTD",
        "ACNS", "WiSec", "SecureComm", "TrustCom", "ISPEC",
        "FPL", "FCCM", "VTS", "CODES+ISSS", "CASES", "SCOPES",
    ],
    # A类期刊
    "A_journal": [
        "TPAMI", "IJCV", "AIJ", "JMLR", "TIT", "JSAC", "TOCS", "TSE",
        "ACM Computing Surveys", "IEEE TIFS", "TDSC", "JCSS",
        "IEEE Transactions on Information Forensics and Security",
        "IEEE Transactions on Dependable and Secure Computing",
    ],
    # B类期刊
    "B_journal": [
        "TNNLS", "TCYB", "TAC", "TRO", "TAP", "JAIR", "Machine Learning",
        "Neural Computation", "TKDE", "TOIS", "TKDD", "TWEB",
        "Neurocomputing", "Pattern Recognition", "Computer Vision and Image Understanding",
        "IEEE Network", "Computer Networks", "ACM TOPS",
        "IEEE Transactions on Services Computing",
    ],
    # C类期刊
    "C_journal": [
        "PRL", "NC", "NN", "IDA", "IJPRAI", "Machine Vision and Applications",
        "DKE", "IS", "KAIS", "WWWJ", "JIIS",
        "Computer Communications", "Wireless Networks",
        "Computers & Security", "JCS", "IJIS",
    ],
}


class JournalRanker:
    """CCF + SCI/JCR 统一分级查询。

    等级映射:
    - A+: CCF-A 且 SCI-Q1
    - A:  CCF-A 或 (CCF-B 且 SCI-Q1/Q2)
    - B:  CCF-B/C 或 SCI-Q3
    - C:  其他
    - None: 未分级

    用法:
        ranker = JournalRanker()
        level = ranker.rank("IEEE S&P")
        # → "A+"
    """

    def __init__(self, data_cache_path: Optional[Path] = None):
        self._ccf = _CCF_DATA
        self._cache_path = data_cache_path or Path(
            "~/.paper_search/journal_ranks.json"
        ).expanduser()

    def rank(self, venue: str) -> str:
        """查询期刊/会议的统一分级。

        Args:
            venue: 期刊或会议名称。

        Returns:
            "A+" / "A" / "B" / "C" / None
        """
        if not venue or venue.lower() in ("arxiv preprint", "arxiv.org", "unknown", "ssrn"):
            return None

        # 清理名称
        venue_clean = self._clean(venue)

        # 先精确匹配 CCF
        ccf_level = self._match_ccf(venue_clean)
        # 再估计 SCI 分区
        sci_zone = self._estimate_sci(venue_clean, ccf_level)

        return self._unified_level(ccf_level, sci_zone)

    def _clean(self, name: str) -> str:
        """清理会议/期刊名称。"""
        # 去除括号内容
        name = re.sub(r"\([^)]*\)", "", name).strip()
        # 统一空格
        name = re.sub(r"\s+", " ", name)
        return name

    def _match_ccf(self, venue: str) -> Optional[str]:
        """模糊匹配 CCF 目录。"""
        venue_lower = venue.lower()

        for cat, names in self._ccf.items():
            for n in names:
                n_lower = n.lower()
                # 精确包含 (要求词边界或完整单词匹配)
                if len(n_lower) >= 3 and (n_lower in venue_lower or venue_lower in n_lower):
                    if len(n_lower) <= 5:
                        # 短名称：检查前后有空格/标点/字符串边界
                        idx = venue_lower.find(n_lower)
                        if idx >= 0:
                            before_ok = idx == 0 or not venue_lower[idx-1].isalpha()
                            after_ok = idx + len(n_lower) >= len(venue_lower) or not venue_lower[idx + len(n_lower)].isalpha()
                            if before_ok and after_ok:
                                return cat.split("_")[0]
                    else:
                        return cat.split("_")[0]
                # 模糊匹配
                if SequenceMatcher(None, venue_lower[:30], n_lower[:30]).ratio() > 0.8:
                    return cat.split("_")[0]
                # 缩写匹配 (如 "IEEE S&P" ↔ "IEEE Symposium on Security and Privacy")
                n_parts = set(re.findall(r"\w+", n_lower))
                v_parts = set(re.findall(r"\w+", venue_lower))
                # 至少3个重叠且占比>50%
                if len(n_parts & v_parts) >= 3 and len(n_parts & v_parts) >= len(n_parts) * 0.5:
                    return cat.split("_")[0]

        return None

    def _estimate_sci(self, venue: str, ccf_level: Optional[str]) -> Optional[str]:
        """估计 SCI 分区（简化版）。"""
        venue_lower = venue.lower()

        # Q1 级别的关键词
        q1_keywords = [
            "ieee transactions on", "acm transactions on",
            "nature", "science", "cell", "lancet",
            "proceedings of the ieee", "ieee journal on",
            "acm computing surveys",
        ]
        # Q2 级别的关键词
        q2_keywords = [
            "ieee journal", "elsevier", "springer",
            "journal of", "acm computing",
        ]
        for kw in q1_keywords:
            if kw in venue_lower:
                return "Q1"
        for kw in q2_keywords:
            if kw in venue_lower:
                return "Q2"

        # 基于 CCF 推测（仅当未匹配到关键词时）
        if ccf_level == "A":
            return "Q1"
        elif ccf_level == "B":
            return "Q2"
        elif ccf_level == "C":
            return "Q3"
        return None

    def _is_conference(self, venue: str) -> bool:
        """判断是否是会议（CCF分级适用）。"""
        conf_keywords = [
            "conference", "symposium", "workshop", "proceedings",
            "aaai", "neurips", "icml", "ijcai", "cvpr", "iccv",
            "acl", "emnlp", "iclr", "eccv", "icra", "iros",
        ]
        venue_lower = venue.lower()
        return any(kw in venue_lower for kw in conf_keywords)

    def _unified_level(self, ccf: Optional[str], sci: Optional[str]) -> Optional[str]:
        """合并 CCF + SCI → 统一等级。

        A+: CCF-A 且 SCI-Q1 (顶会顶刊)
        A:  CCF-A 或 SCI-Q1
        B:  CCF-B 或 SCI-Q2
        C:  CCF-C 或 SCI-Q3/Q4
        """
        if ccf == "A" and sci == "Q1":
            return "A+"
        if ccf == "A" or sci == "Q1":
            return "A"
        if ccf == "B" or sci == "Q2":
            return "B"
        if ccf == "C" or sci in ("Q3", "Q4"):
            return "C"
        return None

    def rank_batch(self, venues: list[str]) -> list[Optional[str]]:
        return [self.rank(v) for v in venues]
