import json
import re

class PayloadExtractor:
    @classmethod
    def extract(cls, raw: str) -> dict:
        raw = raw.strip()

        result = cls._try_parse(raw)
        if result is not None:
            return {"success": True, "payload": result, "strategy": "direct"}

        code_block = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if code_block:
            result = cls._try_parse(code_block.group(1).strip())
            if result is not None:
                return {"success": True, "payload": result, "strategy": "code_block"}

        brace = cls._extract_balanced(raw, "{", "}")
        if brace is not None:
            result = cls._try_parse(brace)
            if result is not None:
                return {"success": True, "payload": result, "strategy": "brace_scan"}

        bracket = cls._extract_balanced(raw, "[", "]")
        if bracket is not None:
            result = cls._try_parse(bracket)
            if result is not None:
                return {"success": True, "payload": result, "strategy": "bracket_scan"}

        return {
            "success": False,
            "error": {
                "error_type": "invalid_json_payload",
                "message": "Could not extract valid JSON from LLM output"
            }
        }
    
    @staticmethod
    def _try_parse(text: str):
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _extract_balanced(text: str, open_char: str, close_char: str):
        start = text.find(open_char)
        if start == -1:
            return None

        depth = 0
        in_string = False
        escape_next = False

        for i in range(start, len(text)):
            ch = text[i]

            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue

            if ch == open_char:
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]

        return None
