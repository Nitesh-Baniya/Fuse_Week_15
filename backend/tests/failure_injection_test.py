"""
Failure injection test for the agentic verification system.

This test intentionally introduces failures to evaluate how the agent responds:
- Tool unavailability
- Malformed retrieval output
- Timeouts
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.assistant.verification_agent import VerificationAssistantAgent
from app.core.config import Settings
from app.llm.client import LLMClient
from app.schemas.chat import ChatMessage
from app.tools.builtin import create_default_tool_registry
from app.tools.registry import RegisteredTool, ToolRegistry, ToolHandler
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class FailureType(Enum):
    """Types of failures to inject."""
    TOOL_UNAVAILABLE = "tool_unavailable"
    MALFORMED_OUTPUT = "malformed_output"
    TIMEOUT = "timeout"


@dataclass
class FailureInjectionTest:
    """Configuration for a failure injection test."""
    name: str
    failure_type: FailureType
    test_query: str
    description: str


@dataclass
class FailureTestResult:
    """Results from a failure injection test."""
    test: FailureInjectionTest
    failure_recognized: bool
    response_appropriate: bool
    error_message: str
    agent_response: str
    recovery_attempted: bool


class FailingToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    should_fail: bool = Field(default=True, description="Whether this tool call should fail")


class FailureInjectionTestHarness:
    """
    Test harness for injecting failures into the agentic system.
    
    This evaluates how the agent responds to:
    1. Tool unavailability
    2. Malformed retrieval output
    3. Timeouts
    """

    def __init__(
        self,
        llm_client: LLMClient,
        settings: Settings,
    ) -> None:
        self._llm = llm_client
        self._settings = settings
        self._test_results: list[FailureTestResult] = []

    def _create_failing_tool_registry(self, failure_type: FailureType) -> ToolRegistry:
        """Create a tool registry with failing tools based on the failure type."""
        
        if failure_type == FailureType.TOOL_UNAVAILABLE:
            # Remove a critical tool
            base_registry = create_default_tool_registry(self._settings)
            # Remove web_search to test tool unavailability
            failing_tools = [tool for tool in base_registry._tools.values() 
                           if tool.name != "web_search"]
            return ToolRegistry(tools=failing_tools)
        
        elif failure_type == FailureType.MALFORMED_OUTPUT:
            # Add a tool that returns malformed output
            base_registry = create_default_tool_registry(self._settings)
            
            async def malformed_handler(input_data: BaseModel) -> str:
                return "INVALID JSON {{{{{"
            
            malformed_tool = RegisteredTool(
                name="malformed_search",
                description="A tool that returns malformed output",
                input_model=FailingToolInput,
                handler=malformed_handler,
            )
            
            # Add the failing tool to the registry
            all_tools = list(base_registry._tools.values()) + [malformed_tool]
            return ToolRegistry(tools=all_tools)
        
        elif failure_type == FailureType.TIMEOUT:
            # Add a tool that times out
            base_registry = create_default_tool_registry(self._settings)
            
            async def timeout_handler(input_data: BaseModel) -> str:
                await asyncio.sleep(30)  # Long delay to simulate timeout
                return "This should timeout"
            
            timeout_tool = RegisteredTool(
                name="timeout_search",
                description="A tool that times out",
                input_model=FailingToolInput,
                handler=timeout_handler,
            )
            
            all_tools = list(base_registry._tools.values()) + [timeout_tool]
            return ToolRegistry(tools=all_tools)
        
        else:
            return create_default_tool_registry(self._settings)

    async def run_failure_test(self, test: FailureInjectionTest) -> FailureTestResult:
        """Run a single failure injection test."""
        
        logger.info(f"Running failure test: {test.name}")
        logger.info(f"Failure type: {test.failure_type.value}")
        logger.info(f"Description: {test.description}")
        
        tool_registry = self._create_failing_tool_registry(test.failure_type)
        agent = VerificationAssistantAgent(
            llm_client=self._llm,
            tool_registry=tool_registry,
            settings=self._settings,
        )
        
        failure_recognized = False
        response_appropriate = False
        error_message = ""
        agent_response = ""
        recovery_attempted = False
        
        try:
            # Run with a timeout to handle the timeout failure case
            result = await asyncio.wait_for(
                agent.run(
                    question=test.test_query,
                    history=[],
                    context=None,
                ),
                timeout=10.0,  # 10 second timeout for the whole operation
            )
            
            agent_response = result.output.answer
            
            # Analyze the response
            failure_recognized, response_appropriate, recovery_attempted = self._analyze_response(
                test,
                agent_response,
                result.tools_used,
            )
            
            logger.info(f"Test completed. Response length: {len(agent_response)}")
            
        except asyncio.TimeoutError:
            error_message = "Operation timed out"
            failure_recognized = True  # Timeout was recognized
            response_appropriate = False  # But not handled gracefully
            agent_response = ""
            logger.warning(f"Test timed out: {test.name}")
            
        except Exception as exc:
            error_message = str(exc)
            agent_response = ""
            
            # Check if the error was handled appropriately
            failure_recognized = "tool" in str(exc).lower() or "unavailable" in str(exc).lower()
            response_appropriate = failure_recognized  # If recognized, response is appropriate
            
            logger.warning(f"Test failed with exception: {exc}")
        
        return FailureTestResult(
            test=test,
            failure_recognized=failure_recognized,
            response_appropriate=response_appropriate,
            error_message=error_message,
            agent_response=agent_response,
            recovery_attempted=recovery_attempted,
        )

    def _analyze_response(
        self,
        test: FailureInjectionTest,
        response: str,
        tools_used: list[Any],
    ) -> tuple[bool, bool, bool]:
        """Analyze the agent's response to a failure."""
        
        failure_recognized = False
        response_appropriate = False
        recovery_attempted = False
        
        response_lower = response.lower()
        
        # Check if failure was recognized
        failure_indicators = [
            "error", "unavailable", "failed", "timeout", "cannot", "unable",
            "sorry", "apologize", "issue", "problem"
        ]
        
        failure_recognized = any(indicator in response_lower for indicator in failure_indicators)
        
        # Check if response is appropriate
        if test.failure_type == FailureType.TOOL_UNAVAILABLE:
            # Appropriate: Acknowledges missing tool and provides alternative
            response_appropriate = (
                failure_recognized and 
                ("without" in response_lower or "alternative" in response_lower or 
                 "unavailable" in response_lower)
            )
            recovery_attempted = "search" in response_lower or "verify" in response_lower
            
        elif test.failure_type == FailureType.MALFORMED_OUTPUT:
            # Appropriate: Handles error gracefully
            response_appropriate = (
                failure_recognized and
                ("error" in response_lower or "invalid" in response_lower)
            )
            recovery_attempted = "retry" in response_lower or "attempt" in response_lower
            
        elif test.failure_type == FailureType.TIMEOUT:
            # Appropriate: Acknowledges timeout
            response_appropriate = (
                failure_recognized and
                ("timeout" in response_lower or "time" in response_lower)
            )
            recovery_attempted = False  # Timeout typically can't be recovered from
        
        return failure_recognized, response_appropriate, recovery_attempted

    def get_failure_tests(self) -> list[FailureInjectionTest]:
        """Get the defined failure injection tests."""
        
        return [
            FailureInjectionTest(
                name="Tool Unavailability - Web Search Missing",
                failure_type=FailureType.TOOL_UNAVAILABLE,
                test_query="What are the latest developments in renewable energy? Please verify this information.",
                description="Tests response when web_search tool is unavailable during verification"
            ),
            FailureInjectionTest(
                name="Malformed Output - Invalid JSON Response",
                failure_type=FailureType.MALFORMED_OUTPUT,
                test_query="Use the malformed_search tool to find information about electric cars",
                description="Tests response when a tool returns malformed JSON output"
            ),
            FailureInjectionTest(
                name="Timeout - Slow Tool Response",
                failure_type=FailureType.TIMEOUT,
                test_query="Use the timeout_search tool to get weather information",
                description="Tests response when a tool takes too long to respond"
            ),
        ]

    async def run_all_failure_tests(self) -> list[FailureTestResult]:
        """Run all failure injection tests."""
        
        tests = self.get_failure_tests()
        results = []
        
        logger.info(f"Starting {len(tests)} failure injection tests")
        
        for test in tests:
            try:
                result = await self.run_failure_test(test)
                results.append(result)
                
                status = "✓" if result.response_appropriate else "✗"
                logger.info(
                    f"{status} {test.name}: "
                    f"Recognized={result.failure_recognized}, "
                    f"Appropriate={result.response_appropriate}"
                )
                
            except Exception as exc:
                logger.error(f"Failure test execution failed: {exc}")
                # Create a failure result
                results.append(FailureTestResult(
                    test=test,
                    failure_recognized=False,
                    response_appropriate=False,
                    error_message=f"Test execution failed: {exc}",
                    agent_response="",
                    recovery_attempted=False,
                ))
        
        self._test_results = results
        return results

    def generate_failure_report(self) -> str:
        """Generate a report on failure injection test results."""
        
        lines = [
            "# Failure Injection Test Report",
            "",
            "## Test Results",
            ""
        ]
        
        for result in self._test_results:
            status = "✓ PASS" if result.response_appropriate else "✗ FAIL"
            lines.append(f"### {result.test.name}")
            lines.append(f"**Status**: {status}")
            lines.append(f"**Failure Type**: {result.test.failure_type.value}")
            lines.append(f"**Description**: {result.test.description}")
            lines.append(f"**Test Query**: {result.test.test_query}")
            lines.append(f"**Failure Recognized**: {result.failure_recognized}")
            lines.append(f"**Response Appropriate**: {result.response_appropriate}")
            lines.append(f"**Recovery Attempted**: {result.recovery_attempted}")
            
            if result.error_message:
                lines.append(f"**Error Message**: {result.error_message}")
            
            if result.agent_response:
                lines.append(f"**Agent Response**: {result.agent_response[:200]}...")
            
            lines.append("")
        
        # Summary
        total_tests = len(self._test_results)
        passed_tests = sum(1 for r in self._test_results if r.response_appropriate)
        recognized_failures = sum(1 for r in self._test_results if r.failure_recognized)
        
        lines.extend([
            "## Summary",
            f"- Total Tests: {total_tests}",
            f"- Passed Tests: {passed_tests}",
            f"- Failure Recognition Rate: {recognized_failures/total_tests:.2%}" if total_tests > 0 else "- Failure Recognition Rate: N/A",
            f"- Appropriate Response Rate: {passed_tests/total_tests:.2%}" if total_tests > 0 else "- Appropriate Response Rate: N/A",
        ])
        
        return "\n".join(lines)


async def main():
    """Main entry point for running failure injection tests."""
    
    logging.basicConfig(level=logging.INFO)
    
    # Initialize components
    settings = Settings()
    llm_client = LLMClient(settings)
    
    # Create and run failure injection test harness
    harness = FailureInjectionTestHarness(llm_client, settings)
    results = await harness.run_all_failure_tests()
    
    # Generate and display report
    report = harness.generate_failure_report()
    print(report)
    
    # Save report to file
    output_path = "failure_injection_results.md"
    with open(output_path, "w") as f:
        f.write(report)
    
    logger.info(f"Failure injection report saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())