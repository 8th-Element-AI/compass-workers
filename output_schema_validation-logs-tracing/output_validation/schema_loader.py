import json

from pathlib import Path
from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError
from shared.logger import get_logger

logger = get_logger(__name__)

class SchemaLoader:
    SCHEMA_DIR = Path(__file__).parent.parent / "schemas"
    @classmethod
    def load_schema(cls, schema_name):
        schema_path = cls.SCHEMA_DIR / f"{schema_name}.json"

        logger.info("Loading schema '%s' from %s", schema_name, schema_path)

        if not schema_path.exists():
            logger.error("Schema '%s' not found at path: %s", schema_name, schema_path)
            return {
                "success": False,
                "error": {
                    "error_type": "schema_not_found",
                    "message": f"Schema '{schema_name}' not found"
                }
            }

        try:
            with open(schema_path, "r") as f:
                schema= json.load(f)
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in schema file '%s': %s", schema_name, e)
            return {
                "success": False,
                "error": {
                    "error_type": "invalid_json_schema",
                    "message": str(e)
                }
            }

        try:
            Draft7Validator.check_schema(schema)
        except SchemaError as e:
            logger.error("Invalid schema structure in '%s': %s", schema_name, e.message)
            return {
                "success": False,
                "error": {
                    "error_type": "invalid_schema",
                    "field": ".".join(map(str,e.path)),
                    "violation_type": "invalid_schema_definition",
                    "message": e.message,
                    "actual": e.instance
                }
            }

        logger.info("Schema '%s' loaded successfully", schema_name)
        return {
            "success": True,
            "schema": schema
        }