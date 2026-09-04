# AI Assistant System Architecture

This project is a full-stack AI assistant application that combines retrieval-augmented generation (RAG) with tool calling capabilities. The system consists of two main components:

- **Frontend**: A Next.js application that handles user interaction, authentication, and real-time chat streaming
- **Backend**: A FastAPI service that manages authentication, document processing, RAG retrieval, and AI model interactions

## System Overview

The application uses a client-server architecture where the frontend communicates with the backend through HTTP requests and Server-Sent Events (SSE) for real-time streaming.

```mermaid
flowchart LR
    User["User"] --> Client["Next.js Client"]
    Client <-->|"HTTP / JSON + SSE"| API["FastAPI API"]
    API --> PostgreSQL["PostgreSQL"]
    API --> Redis["Redis\nRate Limiting"]
    API --> Qdrant["Qdrant Cloud"]
    API --> LLM["Hugging Face Router / vLLM"]
    API --> External["External APIs\nWeather + Calculator"]
```

**Key Design Decisions:**

- **Authentication**: Uses Google OAuth with HttpOnly cookies. The backend stores only a hash of the session token in PostgreSQL
- **Data Isolation**: All user data (sessions, messages, documents) is scoped to the authenticated user ID
- **Rate Limiting**: Redis enforces a limit of 10 chat requests per user per 60 seconds
- **API Versioning**: All backend endpoints use the `/api/v1` prefix

---

## Backend Component Architecture

The backend is organized into several interconnected services that handle different aspects of the application:

```mermaid
flowchart LR
    Client["Next.js"] --> Router["FastAPI routes"]
    Router --> Auth["Auth + rate limit"]
    Router --> Sessions["Sessions"]
    Router --> Chat["Chat"]
    Router --> Documents["Documents"]
    Sessions --> DB["PostgreSQL"]
    Documents --> Ingestion["Ingestion"]
    Ingestion --> DB
    Ingestion --> Qdrant["Qdrant"]
    Chat --> Service["Chat service"]
    Service --> DB
    Service --> Retriever["Retriever"]
    Service --> Agent["Agent"]
    Retriever --> Qdrant
    Agent --> LLM["LLM client"]
    Agent --> Tools["Tool registry"]
```

**Component Responsibilities:**

- **API Routes**: Handle HTTP requests, enforce authentication, apply rate limits, and manage error responses
- **Session Service**: Manage chat session lifecycle (create, list, rename, delete)
- **Chat Service**: Orchestrate the complete chat flow including history loading, RAG retrieval, and AI generation
- **Ingestion Service**: Process uploaded documents (validation, text extraction, chunking, vectorization)
- **Retriever**: Search vector database for relevant document chunks based on user queries
- **Assistant Agent**: Execute the AI model with tool-calling capabilities and structured output
- **Tool Registry**: Manage available tools (calculator, weather, time) and validate their execution
- **LLM Client**: Interface with language model providers (Hugging Face or local vLLM)
- **Databases**: PostgreSQL for persistent data, Qdrant for vector storage, Redis for rate limiting

---

## Database Schema

The database uses PostgreSQL with the following entity relationships:

```mermaid
erDiagram
    USERS ||--o{ AUTH_SESSIONS : authenticates
    USERS ||--o{ CHAT_SESSIONS : owns
    USERS ||--o{ DOCUMENTS : uploads
    CHAT_SESSIONS ||--o{ CHAT_MESSAGES : contains
    CHAT_MESSAGES ||--o{ MESSAGE_DOCUMENTS : attaches
    DOCUMENTS ||--o{ MESSAGE_DOCUMENTS : references
    USERS {
        uuid id PK
        string email
        string provider_subject
    }
    AUTH_SESSIONS {
        uuid id PK
        uuid user_id FK
        string token_hash
        datetime expires_at
    }
    CHAT_SESSIONS {
        uuid id PK
        uuid user_id FK
        string title
        boolean use_rag
    }
    CHAT_MESSAGES {
        uuid id PK
        uuid session_id FK
        string role
        string status
        text content
        json details
    }
    DOCUMENTS {
        uuid id PK
        uuid user_id FK
        string status
        datetime expires_at
    }
```

**Data Ownership Model:**

- Every piece of data is linked to a specific user through foreign key relationships
- Cascade deletion ensures that when a user is deleted, all their sessions, messages, and documents are removed
- Sessions can be renamed or deleted through the API
- Documents can be attached to specific messages within a session

---

## User Interaction Flow

The following sequence shows how a user interacts with the system from sign-in to sending a message:

```mermaid
sequenceDiagram
    actor User
    participant UI as Next.js Client
    participant API as FastAPI API
    participant DB as PostgreSQL
    User->>UI: Sign in with Google
    UI->>API: POST /api/v1/auth/google
    API->>DB: Create or find user and session
    API-->>UI: User profile + HttpOnly cookie
    UI->>API: GET /api/v1/sessions
    API->>DB: Load owned session summaries
    API-->>UI: Session list
    User->>UI: Rename chat
    UI->>API: PATCH /api/v1/sessions/{id}
    API->>DB: Verify ownership and update title
    API-->>UI: Updated session summary
    User->>UI: Send message
    UI->>API: POST /api/v1/chat/stream
    API-->>UI: SSE status, tool, delta, and complete events
```

**Frontend State Management:**

- Chat sessions are loaded from the API and stored in React state
- Session details are loaded lazily when a session is selected
- The active session is kept in memory during the chat
- When generation is stopped, the frontend aborts the request but retains received text
- The backend marks stopped messages with status `stopped`

---

## Document Processing Pipeline

Documents go through an ingestion pipeline when uploaded and a retrieval pipeline when referenced in chat:

```mermaid
flowchart TD
    subgraph Ingest["Document Ingestion"]
        Upload["MD, TXT, or PDF"] --> Validate["Validate file"]
        Validate --> Extract["Extract text and chunk"]
        Extract --> Vectors["Create embeddings"]
        Vectors --> Metadata["Store in PostgreSQL"]
        Vectors --> Points["Store in Qdrant with user_id"]
    end
    subgraph Retrieve["Document Retrieval"]
        Question["User question"] --> Filter["Filter by user's documents"]
        Filter --> Candidates["Hybrid search"]
        Candidates --> Rerank["ColBERT reranking"]
        Rerank --> Context["Add to AI context"]
        Candidates -. "Cloud unavailable" .-> Local["Local fallback"]
        Local --> Context
    end
    Points --> Filter
    Cleanup["Background cleanup"] --> DeleteDB["Remove from PostgreSQL"]
    Cleanup --> DeleteQdrant["Remove from Qdrant"]
    Metadata -. "expired" .-> DeleteDB
    Points -. "expired" .-> DeleteQdrant
```

**Ingestion Process:**

- Supports single and batch file uploads (Markdown, text, and PDF)
- Documents are validated, text is extracted, and content is split into overlapping chunks
- Each chunk is converted to a vector embedding and stored in Qdrant
- Metadata (filename, status, expiry) is stored in PostgreSQL
- Documents are scoped to the authenticated user and deduplicated by content hash
- A background worker automatically removes expired documents from both databases

**Retrieval Process:**

- The chat service verifies that requested documents belong to the authenticated user
- Only validated document IDs are passed to the retriever (preventing cross-user access)
- Qdrant performs hybrid search (dense + sparse) with ColBERT reranking
- If cloud inference is unavailable, the system falls back to local dense embeddings
- Retrieved chunks are formatted and added to the AI model's context

---

## AI Agent Execution Flow

The AI agent uses a tool-calling approach to answer questions with the help of external utilities:

```mermaid
flowchart TD
    Input["Question + History + RAG Context"] --> Planner["Tool-Planning Call"]
    Planner --> Decision{"Tool Calls Needed?"}
    Decision -->|"Yes"| Execute["Validate and Execute Tools"]
    Execute --> Result["Add tool result to context"]
    Result --> Planner
    Decision -->|"No"| Stream["Generate final answer"]
    Stream --> Metadata["Extract metadata"]
    Metadata --> Validate["Validate citations and confidence"]
    Validate --> Response["Save and return response"]
```

**Agent Behavior:**

- The agent can run up to 15 tool-planning rounds (configurable via `LLM_MAX_TOOL_ITERATIONS`)
- Available tools include: calculator, UTC time, weather information
- Tool arguments are validated using Pydantic schemas
- Failed tool calls are returned as explicit results, allowing the model to retry or try a different tool
- The final answer is generated in a separate request without tool definitions
- After the answer stream completes, a separate request extracts structured metadata (citations, confidence scores, follow-up questions)
- Metadata is validated before the response is marked complete

---

## Streaming Chat Implementation

The chat endpoint uses Server-Sent Events (SSE) to stream responses in real-time:

```mermaid
sequenceDiagram
    participant Client
    participant Chat as ChatService
    participant Agent
    participant Provider as LLM Provider
    Client->>Chat: POST /api/v1/chat/stream
    Chat->>Chat: Persist pending messages
    Chat-->>Client: status: retrieving
    Chat->>Agent: Question + history + context
    Agent->>Provider: Tool-planning request
    Provider-->>Agent: Tool call decision
    Agent-->>Chat: Tool execution event
    Chat-->>Client: tool event
    Agent->>Provider: Stream final answer
    Provider-->>Client: delta events
    Agent->>Provider: Metadata request
    Provider-->>Chat: Validated metadata
    Chat-->>Client: complete event
```

**SSE Event Types:**

| Event | Description |
| --- | --- |
| `status` | Current progress (retrieving, generating, etc.) |
| `tool` | Result of a tool execution |
| `delta` | Fragment of the streaming answer text |
| `complete` | Final validated answer with metadata |
| `error` | Recoverable error during processing |

**Request Lifecycle:**

1. Request passes authentication and Redis rate limit (10 requests/60 seconds per user)
2. Chat service verifies session ownership and document permissions
3. Pending user and assistant messages are persisted to PostgreSQL
4. Document context is retrieved and passed to the agent
5. Agent executes tools and streams the answer
6. After streaming completes, metadata is extracted and validated
7. Complete message is persisted and `complete` event is emitted

**Frontend Event Handling:**

- `status`: Updates the activity indicator
- `tool`: Displays tool execution results
- `delta`: Appends text to the visible answer
- `complete`: Replaces draft with final answer including citations and metadata
- `error`: Marks the message as failed
- Stop button uses AbortController to cancel requests; received text is preserved with status `stopped`

---

## Language Model Configuration

The system supports two backend options for language models:

```mermaid
flowchart LR
    Request["LLM Request"] --> Backend{"LLM_BACKEND"}
    Backend -->|"huggingface"| Primary["Primary Model"]
    Primary -. "Failure before streaming" .-> Fallback["Fallback Model"]
    Backend -->|"vllm"| Local["Local vLLM Endpoint"]
    Primary --> Answer["Response"]
    Fallback --> Answer
    Local --> Answer
```

**Backend Options:**

- **Hugging Face**: Uses hosted models through the Hugging Face Router with automatic fallback
- **vLLM**: Uses a local or remote OpenAI-compatible endpoint (useful for GPU acceleration)

**Failure Handling:**

- If the primary model fails before streaming starts, the system automatically retries with the fallback model
- Once streaming begins, partial output is never mixed with a different model
- The weather API client includes a retry mechanism with timeout handling
- Tool planning is bounded to prevent infinite loops

---

## API Endpoints

The backend exposes the following HTTP endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/health` | Health check for monitoring |
| `POST` | `/api/v1/auth/google` | Authenticate with Google OAuth |
| `GET` | `/api/v1/auth/me` | Get current user information |
| `POST` | `/api/v1/auth/logout` | End the current session |
| `GET` | `/api/v1/sessions` | List all chat sessions |
| `POST` | `/api/v1/sessions` | Create a new chat session |
| `GET` | `/api/v1/sessions/{id}` | Get session details |
| `PATCH` | `/api/v1/sessions/{id}` | Rename a session |
| `DELETE` | `/api/v1/sessions/{id}` | Delete a session |
| `POST` | `/api/v1/documents` | Upload a single document |
| `POST` | `/api/v1/documents/batch` | Upload multiple documents |
| `POST` | `/api/v1/chat` | Send a chat message (non-streaming) |
| `POST` | `/api/v1/chat/stream` | Send a chat message (streaming) |

**Security Features:**

- All protected endpoints require authentication via HttpOnly cookie
- Chat requests are rate-limited per user using Redis
- Document access is validated to ensure users can only access their own files
- Errors are returned as HTTP errors for non-streaming endpoints or SSE events for streaming

---

## Deployment Architecture

The system can be deployed using Docker Compose with the following components:

```mermaid
flowchart LR
    Browser["Browser"] --> Web["Next.js Application"]
    Web --> API["FastAPI Container"]
    API --> DB["PostgreSQL"]
    API --> Redis["Redis"]
    API --> Qdrant["Qdrant Cloud"]
    API --> Hosted["Hugging Face Router"]
    API -. "Optional GPU profile" .-> VLLM["vLLM Service"]
```

**Deployment Options:**

- **Standard**: Runs the API, frontend, PostgreSQL, Redis, and connects to hosted Qdrant and Hugging Face
- **Local GPU**: Adds a local vLLM service for GPU-accelerated inference (requires NVIDIA GPU)
- **Colab**: Can use Google Colab GPU with ngrok to expose a local vLLM endpoint

**Configuration:**

- All settings are loaded from `.env` files using Pydantic
- Sensitive values (API keys, database URLs) are never committed to source control
- The same configuration works for both local development and production deployment

---

## System Design Principles

**Data Authority:**

- PostgreSQL is the single source of truth for all user data, sessions, messages, and document metadata
- Qdrant stores vector embeddings and is synchronized with PostgreSQL document records
- Redis is used only for rate limiting and can be cleared without data loss
- The backend always loads chat history from PostgreSQL, not from client submissions

**Security and Validation:**

- All operations are scoped to the authenticated user ID
- Document access is validated before retrieval to prevent cross-user data access
- Tool inputs and file uploads are validated before processing
- Session tokens are stored as hashes in the database

**Database Management:**

- Schema changes are handled through Alembic migrations, not application startup
- Expired documents are automatically removed from both PostgreSQL and Qdrant
- Cascade deletion ensures data consistency when users are removed
