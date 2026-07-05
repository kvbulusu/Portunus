"""
llm_interface.py — Abacus.AI LLM integration for the JANUS ASIC agent.

When an ORFS stage fails, the orchestrator sends a compact error excerpt here.
This module asks an Abacus-hosted LLM for a STRUCTURED, machine-applicable fix
and returns it as a dict the FixApplier can act on.

Design goals:
  * Use the official `abacusai` Python SDK (client.evaluate_prompt) when present.
  * Fall back to a raw REST POST if the SDK is unavailable.
  * Strict rate limiting + retry/backoff so we never spam the API (saves credits).
  * Keep request payloads under a byte budget (default 5 KB).
  * Never raise into the orchestrator: on any failure return a result with
    ok=False and a human-readable reason, so the flow degrades gracefully.

Authentication: reads ABACUS_API_KEY from the environment.

The LLM is asked to reply with a JSON object of the shape:

    {
      "diagnosis": "<one-paragraph root cause>",
      "confidence": "high|medium|low",
      "fixes": [
        {"type": "config",     "key": "CORE_UTILIZATION", "value": "50",
         "reason": "..."},
        {"type": "sdc_period", "clock": "core_clk", "value": "1.50",
         "reason": "relax target to meet timing"},
        {"type": "rtl",        "file": "rtl/...sv", "find": "...", "replace": "...",
         "reason": "..."},
        {"type": "manual",     "instruction": "<human must do this>"}
      ]
    }

Only `config`, `sdc_period`, and `rtl` fixes are auto-applied; `manual` fixes are
surfaced to the user.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


SYSTEM_PROMPT = (
    "You are an expert ASIC physical-design engineer assisting an autonomous "
    "OpenROAD-flow-scripts (ORFS) run on the ASAP7 7nm PDK. You are given a "
    "failing stage's log excerpt for one RTL block. Diagnose the ROOT CAUSE and "
    "propose minimal, safe, machine-applicable fixes. Prefer config/constraint "
    "changes over RTL edits. NEVER invent performance numbers. Respond with ONLY "
    "a single JSON object, no prose, matching the schema the user describes."
)

USER_TEMPLATE = """A stage failed in the JANUS ASIC flow. Here is the context.

{excerpt}
{physical}
Reply with ONLY a JSON object:
{{
  "diagnosis": "<root cause, one short paragraph>",
  "confidence": "high|medium|low",
  "fixes": [
    {{"type":"config","key":"<ORFS make var>","value":"<new value>","reason":"..."}},
    {{"type":"sdc_period","clock":"<clock name>","value":"<period ns>","reason":"..."}},
    {{"type":"rtl","file":"<path>","find":"<exact text>","replace":"<new text>","reason":"..."}},
    {{"type":"sram_reshape","macro":"<fakeram name>","banks":<int pow2>,"column_mux_factor":<1|2|4|8>,"reason":"..."}},
    {{"type":"manual","instruction":"<what a human must do>"}}
  ]
}}
Allowed config keys include: CORE_UTILIZATION, PLACE_DENSITY, CORE_ASPECT_RATIO,
CORE_MARGIN, SYNTH_HIERARCHICAL, ABC_AREA. Keep fixes minimal.

For an [ERROR MPL-0006] "macro does not fit in the core" failure, do NOT shrink
utilization as a band-aid: the real cause is a pathological FakeRAM SRAM aspect
ratio (a deep memory laid out as a sliver). Prefer a `sram_reshape` fix that
raises `banks` (and/or global `column_mux_factor`) to make the macro roughly
square. FakeRAM geometry: height = K*depth/F, width = K'*width_bits*F, where
F = banks*column_mux_factor; this leaves the bitcell count and pin interface
UNCHANGED. If the PHYSICAL ANALYSIS block below already lists recommended banks,
echo those values."""


@dataclass
class LLMResult:
    ok: bool
    diagnosis: str = ""
    confidence: str = "low"
    fixes: List[Dict] = field(default_factory=list)
    raw: str = ""
    error: str = ""
    # rough accounting (characters in/out) for credit estimation
    chars_in: int = 0
    chars_out: int = 0


class AbacusLLM:
    def __init__(
        self,
        api_key: Optional[str] = None,
        llm_name: Optional[str] = None,
        min_interval_s: float = 3.0,
        max_retries: int = 3,
        byte_budget: int = 5000,
        logger=None,
    ):
        self.api_key = api_key or os.environ.get("ABACUS_API_KEY", "")
        # llm_name=None lets Abacus route to a default capable model.
        self.llm_name = llm_name or os.environ.get("ABACUS_LLM_NAME") or None
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.byte_budget = byte_budget
        self.log = logger or (lambda *a, **k: None)
        self._last_call_ts = 0.0
        self._client = None
        self.calls_made = 0
        self.total_chars_in = 0
        self.total_chars_out = 0

    # ---- availability -------------------------------------------------------
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import abacusai
            self._client = abacusai.ApiClient(self.api_key)
        except Exception as e:  # SDK missing or bad key — REST fallback handles it
            self.log(f"[llm] abacusai SDK unavailable ({e}); will try REST")
            self._client = None
        return self._client

    # ---- rate limiting ------------------------------------------------------
    def _throttle(self) -> None:
        dt = time.time() - self._last_call_ts
        if dt < self.min_interval_s:
            time.sleep(self.min_interval_s - dt)
        self._last_call_ts = time.time()

    # ---- main entry ---------------------------------------------------------
    def request_fix(self, excerpt: str, physical: str = "",
                    reasoning: str = "") -> LLMResult:
        """Ask the LLM for a fix. `physical` is an optional pre-computed
        deterministic analysis block (e.g. SRAM macro-fit reshape
        recommendations) that we inject verbatim so the model grounds its
        answer in real geometry instead of guessing. `reasoning` is the
        deterministic 'principal-engineer' briefing from reasoning_engine that
        frames the failure as a remedy-LAYER decision with cross-stage
        blast-radius — injected so the model reasons about root cause and cost
        instead of pattern-matching the log."""
        if not self.available():
            return LLMResult(ok=False, error="ABACUS_API_KEY not set")

        excerpt = excerpt.encode("utf-8")[: self.byte_budget].decode("utf-8", "replace")
        phys = ""
        if reasoning:
            phys += ("\n=== REASONING BRIEFING (deterministic classification — "
                     "reason about LAYER + blast radius before you edit) ===\n"
                     + reasoning + "\n")
        if physical:
            phys += "\n=== PHYSICAL ANALYSIS (deterministic, trust this) ===\n" + physical + "\n"
        prompt = USER_TEMPLATE.format(excerpt=excerpt, physical=phys)
        chars_in = len(prompt) + len(SYSTEM_PROMPT)

        last_err = ""
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                raw = self._call(prompt)
                self.calls_made += 1
                self.total_chars_in += chars_in
                self.total_chars_out += len(raw)
                parsed = self._parse(raw)
                parsed.chars_in = chars_in
                parsed.chars_out = len(raw)
                return parsed
            except Exception as e:
                last_err = str(e)
                backoff = min(30.0, self.min_interval_s * (2 ** (attempt - 1)))
                self.log(f"[llm] attempt {attempt}/{self.max_retries} failed: {e}; backoff {backoff:.0f}s")
                time.sleep(backoff)
        return LLMResult(ok=False, error=f"LLM call failed after {self.max_retries} retries: {last_err}")

    # ---- transport ----------------------------------------------------------
    def _call(self, prompt: str) -> str:
        client = self._get_client()
        if client is not None:
            # Preferred path: SDK evaluate_prompt.
            kwargs = dict(prompt=prompt, system_message=SYSTEM_PROMPT)
            if self.llm_name:
                kwargs["llm_name"] = self.llm_name
            resp = client.evaluate_prompt(**kwargs)
            # SDK returns an object with .content (string).
            content = getattr(resp, "content", None)
            if content is None and isinstance(resp, dict):
                content = resp.get("content")
            if not content:
                content = str(resp)
            return content
        return self._call_rest(prompt)

    def _call_rest(self, prompt: str) -> str:
        """Raw REST fallback when the SDK is not importable."""
        import requests
        url = "https://api.abacus.ai/api/v0/evaluatePrompt"
        payload = {"prompt": prompt, "systemMessage": SYSTEM_PROMPT}
        if self.llm_name:
            payload["llmName"] = self.llm_name
        headers = {"apiKey": self.api_key, "Content-Type": "application/json"}
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()
        # Abacus REST wraps results under "result".
        result = data.get("result", data)
        if isinstance(result, dict):
            return result.get("content") or json.dumps(result)
        return str(result)

    # ---- parsing ------------------------------------------------------------
    @staticmethod
    def _parse(raw: str) -> LLMResult:
        obj = AbacusLLM._extract_json(raw)
        if obj is None:
            return LLMResult(ok=False, raw=raw, error="LLM did not return parseable JSON")
        fixes = obj.get("fixes", [])
        if not isinstance(fixes, list):
            fixes = []
        return LLMResult(
            ok=True,
            diagnosis=str(obj.get("diagnosis", "")),
            confidence=str(obj.get("confidence", "low")).lower(),
            fixes=fixes,
            raw=raw,
        )

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict]:
        # 1) direct
        try:
            return json.loads(text)
        except Exception:
            pass
        # 2) fenced ```json ... ```
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
        # 3) first {...} balanced-ish
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
        return None

    # ---- credit estimate ----------------------------------------------------
    def usage(self) -> Dict:
        # Rough token estimate ~4 chars/token. This is an ESTIMATE for budgeting,
        # not a billed figure — check your Abacus dashboard for actual credits.
        est_tokens = (self.total_chars_in + self.total_chars_out) / 4.0
        return {
            "llm_calls": self.calls_made,
            "chars_in": self.total_chars_in,
            "chars_out": self.total_chars_out,
            "approx_tokens": round(est_tokens),
        }


# ---- self-test (offline; checks parsing only) -------------------------------
if __name__ == "__main__":
    demo = '''Sure! ```json
    {"diagnosis":"clock too tight","confidence":"high",
     "fixes":[{"type":"sdc_period","clock":"core_clk","value":"1.50","reason":"relax"}]}
    ```'''
    res = AbacusLLM._parse(demo)
    assert res.ok and res.fixes[0]["type"] == "sdc_period", res
    print("llm_interface parse self-test OK:", res.diagnosis, res.fixes)
    llm = AbacusLLM(api_key="")
    print("available without key:", llm.available())
