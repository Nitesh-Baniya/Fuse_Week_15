# Agentic Verification Architecture

## Enhanced System Architecture with Agentic Verification

```mermaid
flowchart TB
    subgraph UserLayer["User Interface Layer"]
        User[User]
        Frontend[Next.js Frontend]
    end
    
    subgraph APILayer["API Layer"]
        Router[FastAPI Router]
        Auth[Authentication & Rate Limiting]
    end
    
    subgraph ServiceLayer["Service Layer"]
        ChatService[Chat Service]
        SessionService[Session Service]
        DocumentService[Document Service]
    end
    
    subgraph AgentLayer["Agent Layer"]
        VerificationAgent[Verification Agent]
        SkillManager[Skill Manager]
        ToolRegistry[Tool Registry]
    end
    
    subgraph ToolLayer["Tool Layer"]
        WebSearch[Web Search Tool]
        Calculator[Calculator Tool]
        Weather[Weather Tool]
        Time[Time Tool]
    end
    
    subgraph DataLayer["Data Layer"]
        PostgreSQL[(PostgreSQL)]
        Qdrant[(Qdrant Vector DB)]
        Redis[(Redis)]
    end
    
    subgraph ExternalLayer["External Services"]
        HuggingFace[Hugging Face LLM]
        WeatherAPI[Weather API]
        SearchAPI[Search API]
    end
    
    User --> Frontend
    Frontend <--> Router
    Router --> Auth
    Router --> ChatService
    Router --> SessionService
    Router --> DocumentService
    
    ChatService --> PostgreSQL
    ChatService --> VerificationAgent
    SessionService --> PostgreSQL
    DocumentService --> PostgreSQL
    DocumentService --> Qdrant
    
    VerificationAgent --> SkillManager
    VerificationAgent --> ToolRegistry
    VerificationAgent --> HuggingFace
    
    ToolRegistry --> WebSearch
    ToolRegistry --> Calculator
    ToolRegistry --> Weather
    ToolRegistry --> Time
    
    WebSearch --> SearchAPI
    Weather --> WeatherAPI
    
    ChatService --> Redis
    Auth --> Redis
```

## Agentic Verification Loop Detail

```mermaid
flowchart TD
    Start[User Query] --> Skills[Apply Progressive Disclosure Skills]
    Skills --> BuildMessages[Build Messages with Skill Context]
    BuildMessages --> ToolLoop[Tool Planning Loop]
    
    ToolLoop --> LLMCall1[LLM Tool Planning Call]
    LLMCall1 --> ToolDecision{Tools Needed?}
    
    ToolDecision -->|Yes| ExecuteTools[Execute Tools]
    ExecuteTools --> AddResults[Add Results to Context]
    AddResults --> IterationCheck{Max Iterations?}
    IterationCheck -->|No| ToolLoop
    IterationCheck -->|Yes| MaxIterError[Maximum Iterations Error]
    
    ToolDecision -->|No| InitialAnswer[Generate Initial Answer]
    InitialAnswer --> VerifyDecision{Verification Needed?}
    
    VerifyDecision -->|No| FinalResponse[Generate Final Response]
    VerifyDecision -->|Yes| VerifyLoop[Verification Loop]
    
    VerifyLoop --> VerifyLLM[LLM Verification Evaluation]
    VerifyLLM --> GenerateQueries[Generate Search Queries]
    GenerateQueries --> WebSearchCalls[Execute Web Searches]
    WebSearchCalls --> Compare[Compare Results]
    Compare --> VerifyCheck{More Verification?}
    
    VerifyCheck -->|Yes| VerifyStepCheck{Max Verification Steps?}
    VerifyStepCheck -->|No| VerifyLoop
    VerifyStepCheck -->|Yes| UpdateAnswer[Update Answer with Verification]
    
    VerifyCheck -->|No| UpdateAnswer
    UpdateAnswer --> FinalResponse
    
    FinalResponse --> ExtractMetadata[Extract Metadata]
    ExtractMetadata --> Complete[Return Complete Response]
    
    MaxIterError --> Error[Return Error Response]
```

## Multi-Agent vs Single-Agent Decision

### Single-Agent Design (Chosen)

The verification system uses a single-agent design rather than a multi-agent system for the following reasons:

1. **Sequential Nature**: The verification process is inherently sequential (answer → evaluate → search → update), with no natural parallelization opportunities.

2. **Context Coherence**: Maintaining the original question, initial answer, and verification results in a single conversation flow is more straightforward than coordinating state across multiple agents.

3. **Reduced Overhead**: Avoids the complexity of inter-agent communication protocols, message passing, and state synchronization.

4. **No Specialization Benefit**: The verification task doesn't require distinct specialized knowledge bases that would benefit from agent specialization.

### Avoided Structural Failures

- **Sequential Bottleneck**: Single agent avoids the wait times inherent in multi-agent coordination
- **Context Saturation**: Single shared context window instead of multiple agent contexts
- **Skill Dilution**: No dilution of capabilities across multiple agents
- **Single Point of Failure**: Simpler architecture with fewer coordination failure modes

## Context Engineering: Progressive Disclosure

### Implementation

The SkillManager implements progressive disclosure by:

1. **Initial Context**: Loads only 1-2 sentence summaries for each skill (verification, research, comparison)
2. **Trigger Detection**: Analyzes user queries for trigger keywords indicating skill relevance
3. **Dynamic Loading**: Loads full skill instructions only when relevant keywords are detected
4. **State Tracking**: Maintains set of loaded skills to avoid redundant loading

### Benefits

- **Reduced Token Usage**: Initial context is ~70% smaller than loading all full instructions
- **Focused Context**: Only relevant detailed instructions are provided when needed
- **Scalability**: Easy to add new skills without bloating initial context
- **Flexibility**: Skills can be loaded from external files or defined programmatically

### Example

**Without Progressive Disclosure:**
```
System: [Full verification instructions - 200 tokens]
       [Full research instructions - 180 tokens] 
       [Full comparison instructions - 150 tokens]
       [Base system prompt - 300 tokens]
Total: ~830 tokens
```

**With Progressive Disclosure:**
```
System: [Verification summary - 20 tokens]
       [Research summary - 15 tokens]
       [Comparison summary - 15 tokens]
       [Base system prompt - 300 tokens]
Total: ~350 tokens (saves ~480 tokens per query)
```

When verification is triggered, the full instructions (200 tokens) are added, but only for relevant queries.