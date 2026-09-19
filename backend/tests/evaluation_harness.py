"""
Evaluation harness for the agentic verification feature.

This harness tests the cross-source verification capability with metrics including:
- Task completion rate
- Tool-call correctness
- Trajectory length
- Token usage
- Failure classification
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from app.assistant.verification_agent import VerificationAssistantAgent
from app.core.config import Settings
from app.llm.client import LLMClient
from app.schemas.chat import ChatMessage
from app.tools.builtin import create_default_tool_registry
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class FailureType(Enum):
    """Classification of failures based on the course taxonomy."""
    HARD_FAILURE = "hard_failure"  # Complete failure to produce a useful response
    SOFT_FAILURE = "soft_failure"  # Partial success with notable issues
    CASCADING_SOFT_FAILURE = "cascading_soft_failure"  # Multiple compounding soft failures


@dataclass
class TestCase:
    """A single test case for evaluation."""
    query: str
    expected_tools: list[str]  # Tools that should ideally be used
    expected_verifications: bool  # Whether verification should be performed
    expected_min_iterations: int  # Minimum expected iterations
    description: str


@dataclass
class TestResult:
    """Results from running a single test case."""
    test_case: TestCase
    success: bool
    tools_used: list[str]
    verification_performed: bool
    iterations: int
    tokens_used: int
    execution_time: float
    failure_type: FailureType | None = None
    failure_reason: str = ""
    error_message: str = ""


@dataclass
class EvaluationReport:
    """Aggregated evaluation results."""
    total_tests: int
    successful_tests: int
    task_completion_rate: float
    tool_call_correctness: float
    average_iterations: float
    total_tokens: int
    average_tokens_per_query: float
    failure_classification: dict[FailureType, int]
    results: list[TestResult] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class EvaluationHarness:
    """
    Evaluation harness for testing agentic verification features.
    
    This harness measures:
    - Task completion rate: Success rate across test queries
    - Tool-call correctness: Whether appropriate tools were selected with valid arguments
    - Trajectory length: Number of iterations per query
    - Token accounting: Total tokens consumed per query
    - Failure classification: Categorization of unsuccessful cases
    """

    def __init__(
        self,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        settings: Settings,
    ) -> None:
        self._llm = llm_client
        self._tools = tool_registry
        self._settings = settings
        self._agent = VerificationAssistantAgent(
            llm_client=llm_client,
            tool_registry=tool_registry,
            settings=settings,
        )

    def _get_test_cases(self) -> list[TestCase]:
        """Define test cases for evaluation."""
        
        return [
            TestCase(
                query="What is the current weather in Tokyo?",
                expected_tools=["get_current_weather"],
                expected_verifications=True,
                expected_min_iterations=1,
                description="Simple weather query that should trigger verification"
            ),
            TestCase(
                query="Calculate 25 * 17 + 43",
                expected_tools=["calculator"],
                expected_verifications=False,
                expected_min_iterations=1,
                description="Math calculation that doesn't need verification"
            ),
            TestCase(
                query="What time is it now in UTC?",
                expected_tools=["current_time"],
                expected_verifications=False,
                expected_min_iterations=1,
                description="Time query that shouldn't require verification"
            ),
            TestCase(
                query="Compare the weather in New York, London, and Tokyo",
                expected_tools=["get_current_weather", "web_search"],
                expected_verifications=True,
                expected_min_iterations=2,
                description="Multi-location comparison requiring verification"
            ),
            TestCase(
                query="What is the population of Brazil and how does it compare to India?",
                expected_tools=["web_search"],
                expected_verifications=True,
                expected_min_iterations=2,
                description="Population comparison requiring verification"
            ),
            TestCase(
                query="Calculate the area of a circle with radius 5",
                expected_tools=["calculator"],
                expected_verifications=False,
                expected_min_iterations=1,
                description="Geometry calculation without verification need"
            ),
            TestCase(
                query="What are the latest developments in renewable energy?",
                expected_tools=["web_search"],
                expected_verifications=True,
                expected_min_iterations=2,
                description="Current events topic requiring verification"
            ),
            TestCase(
                query="What is the current temperature in Paris and is it raining?",
                expected_tools=["get_current_weather"],
                expected_verifications=True,
                expected_min_iterations=1,
                description="Weather query with specific condition check"
            ),
        ]

    async def run_test_case(self, test_case: TestCase) -> TestResult:
        """Run a single test case and collect metrics."""
        
        start_time = time.time()
        tokens_used = 0
        failure_type = None
        failure_reason = ""
        error_message = ""
        
        try:
            # Run the agent
            result = await self._agent.run(
                question=test_case.query,
                history=[],
                context=None,
            )
            
            execution_time = time.time() - start_time
            
            # Extract metrics
            tools_used = [tool.name for tool in result.tools_used]
            verification_performed = result.verification_steps > 0
            iterations = result.verification_steps + 1  # Initial iteration + verification steps
            
            # Estimate token usage (in production, get actual token counts from LLM client)
            tokens_used = self._estimate_token_usage(
                test_case.query,
                result.output.answer,
                tools_used,
                iterations,
            )
            
            # Evaluate success
            success = self._evaluate_success(
                test_case,
                tools_used,
                verification_performed,
                iterations,
                result.output.answer,
            )
            
            if not success:
                failure_type, failure_reason = self._classify_failure(
                    test_case,
                    tools_used,
                    verification_performed,
                    iterations,
                    result.output.answer,
                )
            
            return TestResult(
                test_case=test_case,
                success=success,
                tools_used=tools_used,
                verification_performed=verification_performed,
                iterations=iterations,
                tokens_used=tokens_used,
                execution_time=execution_time,
                failure_type=failure_type,
                failure_reason=failure_reason,
                error_message=error_message,
            )
            
        except Exception as exc:
            execution_time = time.time() - start_time
            logger.error(f"Test case failed with exception: {exc}")
            
            return TestResult(
                test_case=test_case,
                success=False,
                tools_used=[],
                verification_performed=False,
                iterations=0,
                tokens_used=0,
                execution_time=execution_time,
                failure_type=FailureType.HARD_FAILURE,
                failure_reason=f"Exception during execution: {type(exc).__name__}",
                error_message=str(exc),
            )

    def _estimate_token_usage(
        self,
        query: str,
        answer: str,
        tools_used: list[str],
        iterations: int,
    ) -> int:
        """Estimate token usage for the query."""
        
        # Rough estimation: ~4 characters per token
        query_tokens = len(query) // 4
        answer_tokens = len(answer) // 4
        tool_tokens = len(tools_used) * 50  # Estimate for tool schemas/results
        iteration_overhead = iterations * 100  # Overhead per iteration
        
        return query_tokens + answer_tokens + tool_tokens + iteration_overhead

    def _evaluate_success(
        self,
        test_case: TestCase,
        tools_used: list[str],
        verification_performed: bool,
        iterations: int,
        answer: str,
    ) -> bool:
        """Evaluate whether the test case was successful."""
        
        # Check if answer is non-empty
        if not answer or len(answer) < 10:
            return False
        
        # Check if expected tools were used (at least one)
        if test_case.expected_tools and not any(
            tool in tools_used for tool in test_case.expected_tools
        ):
            return False
        
        # Check verification expectation
        if test_case.expected_verifications and not verification_performed:
            return False
        
        # Check minimum iterations
        if iterations < test_case.expected_min_iterations:
            return False
        
        return True

    def _classify_failure(
        self,
        test_case: TestCase,
        tools_used: list[str],
        verification_performed: bool,
        iterations: int,
        answer: str,
    ) -> tuple[FailureType, str]:
        """Classify the type of failure."""
        
        # Hard failure: No meaningful answer
        if not answer or len(answer) < 10:
            return FailureType.HARD_FAILURE, "No meaningful answer generated"
        
        # Hard failure: Wrong tools entirely
        if test_case.expected_tools and not any(
            tool in tools_used for tool in test_case.expected_tools
        ):
            return FailureType.HARD_FAILURE, f"Expected tools {test_case.expected_tools} but got {tools_used}"
        
        # Soft failure: Verification not performed when expected
        if test_case.expected_verifications and not verification_performed:
            return FailureType.SOFT_FAILURE, "Verification not performed when expected"
        
        # Soft failure: Insufficient iterations
        if iterations < test_case.expected_min_iterations:
            return FailureType.SOFT_FAILURE, f"Insufficient iterations: {iterations} < {test_case.expected_min_iterations}"
        
        # Cascading soft failure: Multiple issues
        issues = []
        if test_case.expected_verifications and not verification_performed:
            issues.append("missing verification")
        if iterations < test_case.expected_min_iterations:
            issues.append("insufficient iterations")
        
        if len(issues) > 1:
            return FailureType.CASCADING_SOFT_FAILURE, f"Multiple issues: {', '.join(issues)}"
        
        return FailureType.SOFT_FAILURE, "Partial success with notable issues"

    async def run_evaluation(self) -> EvaluationReport:
        """Run the complete evaluation suite."""
        
        test_cases = self._get_test_cases()
        results = []
        
        logger.info(f"Starting evaluation with {len(test_cases)} test cases")
        
        for i, test_case in enumerate(test_cases, 1):
            logger.info(f"Running test case {i}/{len(test_cases)}: {test_case.description}")
            
            try:
                result = await self.run_test_case(test_case)
                results.append(result)
                
                status = "✓" if result.success else "✗"
                logger.info(
                    f"{status} Test case {i}: {result.failure_reason if not result.success else 'Success'}"
                )
                
            except Exception as exc:
                logger.error(f"Failed to run test case {i}: {exc}")
                # Create a failure result
                results.append(TestResult(
                    test_case=test_case,
                    success=False,
                    tools_used=[],
                    verification_performed=False,
                    iterations=0,
                    tokens_used=0,
                    execution_time=0.0,
                    failure_type=FailureType.HARD_FAILURE,
                    failure_reason=f"Test execution failed: {exc}",
                    error_message=str(exc),
                ))
        
        # Calculate aggregate metrics
        successful_tests = sum(1 for r in results if r.success)
        task_completion_rate = successful_tests / len(results) if results else 0.0
        
        # Tool call correctness
        tool_correct = 0
        total_tool_evaluations = 0
        for result in results:
            if result.test_case.expected_tools:
                total_tool_evaluations += 1
                if any(tool in result.tools_used for tool in result.test_case.expected_tools):
                    tool_correct += 1
        tool_call_correctness = tool_correct / total_tool_evaluations if total_tool_evaluations > 0 else 0.0
        
        # Average iterations
        avg_iterations = sum(r.iterations for r in results) / len(results) if results else 0.0
        
        # Token metrics
        total_tokens = sum(r.tokens_used for r in results)
        avg_tokens = total_tokens / len(results) if results else 0.0
        
        # Failure classification
        failure_classification: dict[FailureType, int] = {
            FailureType.HARD_FAILURE: 0,
            FailureType.SOFT_FAILURE: 0,
            FailureType.CASCADING_SOFT_FAILURE: 0,
        }
        for result in results:
            if result.failure_type:
                failure_classification[result.failure_type] += 1
        
        return EvaluationReport(
            total_tests=len(results),
            successful_tests=successful_tests,
            task_completion_rate=task_completion_rate,
            tool_call_correctness=tool_call_correctness,
            average_iterations=avg_iterations,
            total_tokens=total_tokens,
            average_tokens_per_query=avg_tokens,
            failure_classification=failure_classification,
            results=results,
        )

    def generate_report(self, report: EvaluationReport) -> str:
        """Generate a human-readable evaluation report."""
        
        lines = [
            "# Agentic Verification Evaluation Report",
            f"Generated: {report.timestamp}",
            "",
            "## Summary",
            f"- Total Tests: {report.total_tests}",
            f"- Successful Tests: {report.successful_tests}",
            f"- Task Completion Rate: {report.task_completion_rate:.2%}",
            f"- Tool Call Correctness: {report.tool_call_correctness:.2%}",
            f"- Average Iterations: {report.average_iterations:.2f}",
            f"- Total Tokens Used: {report.total_tokens}",
            f"- Average Tokens per Query: {report.average_tokens_per_query:.0f}",
            "",
            "## Failure Classification",
        ]
        
        for failure_type, count in report.failure_classification.items():
            lines.append(f"- {failure_type.value}: {count}")
        
        lines.extend([
            "",
            "## Detailed Results",
            ""
        ])
        
        for i, result in enumerate(report.results, 1):
            status = "✓ PASS" if result.success else "✗ FAIL"
            lines.append(f"### Test {i}: {result.test_case.description}")
            lines.append(f"**Status**: {status}")
            lines.append(f"**Query**: {result.test_case.query}")
            lines.append(f"**Tools Used**: {', '.join(result.tools_used) or 'None'}")
            lines.append(f"**Verification Performed**: {result.verification_performed}")
            lines.append(f"**Iterations**: {result.iterations}")
            lines.append(f"**Tokens Used**: {result.tokens_used}")
            lines.append(f"**Execution Time**: {result.execution_time:.2f}s")
            
            if not result.success:
                lines.append(f"**Failure Type**: {result.failure_type.value if result.failure_type else 'Unknown'}")
                lines.append(f"**Failure Reason**: {result.failure_reason}")
                if result.error_message:
                    lines.append(f"**Error**: {result.error_message}")
            
            lines.append("")
        
        return "\n".join(lines)

    async def save_report(self, report: EvaluationReport, output_path: Path) -> None:
        """Save the evaluation report to a file."""
        
        report_text = self.generate_report(report)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_text)
        
        logger.info(f"Evaluation report saved to {output_path}")


async def main():
    """Main entry point for running evaluations."""
    
    logging.basicConfig(level=logging.INFO)
    
    # Initialize components
    settings = Settings()
    llm_client = LLMClient(settings)
    tool_registry = create_default_tool_registry(settings)
    
    # Create and run evaluation harness
    harness = EvaluationHarness(llm_client, tool_registry, settings)
    report = await harness.run_evaluation()
    
    # Generate and save report
    report_text = harness.generate_report(report)
    print(report_text)
    
    # Save to file
    output_path = Path("evaluation_results.md")
    await harness.save_report(report, output_path)


if __name__ == "__main__":
    asyncio.run(main())