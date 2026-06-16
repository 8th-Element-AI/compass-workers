import json
import time
from jsonschema import Draft7Validator

from output_validation.schema_loader import SchemaLoader
from output_validation.payload_extractor import PayloadExtractor
from output_validation.violation_builder import ViolationBuilder
from output_validation.validation_summary import ValidationSummaryGenerator
from shared.logger import get_logger, get_obs, set_schema_context, clear_schema_context

logger = get_logger(__name__)

class StructuredOutputValidator:
    @classmethod
    def validate(cls, payload, schema_name):
        set_schema_context(schema_name)
        obs = get_obs()

        with obs.trace("schema-validation", {"schema_name": schema_name}):
            logger.info("Validation started for schema '%s'", schema_name)

            payload_extract_ms = 0.0
            extract_strategy = None

            if isinstance(payload, str):
                with obs.trace("payload-extract"):
                    t_extract_start = time.perf_counter()
                    extract_result = PayloadExtractor.extract(payload)
                    t_extract_end = time.perf_counter()
                    payload_extract_ms = round((t_extract_end - t_extract_start) * 1000, 3)

                    if not extract_result["success"]:
                        logger.error("JSON extraction failed: %s", extract_result["error"]["message"])
                        obs.set_attribute("is_valid", False)
                        obs.set_attribute("error_type", "invalid_json_payload")
                        clear_schema_context()
                        return {
                            "is_valid": False,
                            "schema_name": schema_name,
                            "error": extract_result["error"],
                            "timing_ms": {"payload_extract_ms": payload_extract_ms, "total_ms": payload_extract_ms},
                        }

                    payload = extract_result["payload"]
                    extract_strategy = extract_result["strategy"]
                    logger.info(
                        "JSON extracted via strategy='%s' in %.3f ms",
                        extract_strategy,
                        payload_extract_ms
                    )

            t0 = time.perf_counter()
            with obs.trace("schema-load"):
                schema_response = SchemaLoader.load_schema(schema_name)
                if not schema_response["success"]:
                    logger.error(
                        "Schema load failed for '%s': [%s] %s",
                        schema_name,
                        schema_response["error"]["error_type"],
                        schema_response["error"]["message"]
                    )
                    obs.set_attribute("is_valid", False)
                    obs.set_attribute("error_type", schema_response["error"]["error_type"])
                    clear_schema_context()
                    return {
                        "is_valid": False,
                        "schema_name": schema_name,
                        "error": schema_response["error"]
                    }

            t1 = time.perf_counter()
            schema = schema_response["schema"]
            validator = Draft7Validator(schema)

            validation_errors = []

            with obs.trace("payload-validation"):
                for error in validator.iter_errors(payload):
                    violation = ViolationBuilder.build(error)
                    validation_errors.append(violation)
                    obs.add_event("violation_found", {
                        "field": violation["field"],
                        "violation_type": violation["violation_type"],
                        "severity": violation["severity"]
                    })
                    if violation["severity"] == "ERROR":
                        logger.error(
                            "Violation [%s] on field '%s': %s",
                            violation["violation_type"],
                            violation["field"],
                            violation["message"]
                        )
                    else:
                        logger.warning(
                            "Violation [%s] on field '%s': %s",
                            violation["violation_type"],
                            violation["field"],
                            violation["message"]
                        )

            t2 = time.perf_counter()
            all_fields = set(schema.get("properties", {}).keys()) | set(schema.get("required", []))
            total_checks = len(all_fields)

            with obs.trace("summary-generation"):
                validation_summary = ValidationSummaryGenerator.generate(
                    total_checks=total_checks,
                    violations=validation_errors
                )

            t3 = time.perf_counter()
            timing_ms = {
                "payload_extract_ms":    payload_extract_ms,
                "schema_load_ms":        round((t1 - t0) * 1000, 3),
                "payload_validation_ms": round((t2 - t1) * 1000, 3),
                "summary_generation_ms": round((t3 - t2) * 1000, 3),
                "total_ms":              round(payload_extract_ms + (t3 - t0) * 1000, 3),
            }
            if extract_strategy:
                timing_ms["extract_strategy"] = extract_strategy

            obs.set_attribute("is_valid", len(validation_errors) == 0)
            obs.set_attribute("health_score", validation_summary["health_score"])
            obs.set_attribute("violation_count", len(validation_errors))

            if len(validation_errors) == 0:
                logger.info(
                    "Validation passed for schema '%s' | health_score=%.1f",
                    schema_name,
                    validation_summary["health_score"]
                )
            else:
                logger.error(
                    "Validation failed for schema '%s' | violations=%d | health_score=%.1f",
                    schema_name,
                    len(validation_errors),
                    validation_summary["health_score"]
                )

            clear_schema_context()
            return {
                "is_valid": len(validation_errors) == 0,
                "schema_name": schema_name,
                "validation_summary": validation_summary,
                "violations": validation_errors,
                "timing_ms": timing_ms,
            }