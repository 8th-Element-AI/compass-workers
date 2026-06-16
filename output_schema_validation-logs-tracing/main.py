import logging
import time
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)

from output_validation.validator import StructuredOutputValidator

payload = {
    "candidate_name": 123,
    "skills": "Python",
    "resume_score": 120,
    "recommendation":
    "selected"
}

t_start = time.perf_counter()
result = (
    StructuredOutputValidator.validate(
        payload=payload,
        schema_name="resume_scoring"
    )
)
t_end = time.perf_counter()

print(result)
print(f"\nTotal time: {(t_end - t_start) * 1000:.2f} ms")