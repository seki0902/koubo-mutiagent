package agentproxy

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
)

const DefaultBaseURL = "http://127.0.0.1:8090"

// ChatRequest is the stable Go → Python payload for the first Agent proxy.
type ChatRequest struct {
	RequestID        string         `json:"request_id"`
	SessionID        string         `json:"session_id"`
	Message          string         `json:"message"`
	ClientMessageID  string         `json:"client_message_id,omitempty"`
	Metadata         map[string]any `json:"metadata,omitempty"`
	GenerationParams map[string]any `json:"generation_params,omitempty"`
}

// Response contains an upstream response without making Go interpret Agent
// content. The handler only maps transport/HTTP errors to the public contract.
type Response struct {
	StatusCode  int
	ContentType string
	Body        []byte
}

type Client struct {
	BaseURL    string
	HTTPClient *http.Client
}

func New(baseURL string, httpClient *http.Client) (*Client, error) {
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	parsed, err := url.Parse(baseURL)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return nil, fmt.Errorf("invalid agent service base URL")
	}
	if httpClient == nil {
		httpClient = &http.Client{}
	}
	return &Client{BaseURL: baseURL, HTTPClient: httpClient}, nil
}

func NewFromEnv() (*Client, error) {
	baseURL := strings.TrimSpace(os.Getenv("AGENT_SERVICE_BASE_URL"))
	if baseURL == "" {
		baseURL = DefaultBaseURL
	}
	return New(baseURL, nil)
}

// StreamError reports an upstream reply that is not a stream.
//
// A rejected request has to reach the browser as the JSON error contract, so
// the caller needs the status and the body.  Expressing that as an error keeps
// "only a 2xx response may be a stream" in the type rather than in a comment,
// and bounds the read: an error message is not allowed to be unbounded.
type StreamError struct {
	StatusCode  int
	ContentType string
	Body        []byte
}

func (e *StreamError) Error() string {
	return fmt.Sprintf("agent stream rejected with status %d", e.StatusCode)
}

func (c *Client) Chat(ctx context.Context, request ChatRequest) (Response, error) {
	httpRequest, err := c.newChatRequest(ctx, "/internal/agent/chat", request)
	if err != nil {
		return Response{}, err
	}

	response, err := c.HTTPClient.Do(httpRequest)
	if err != nil {
		return Response{}, fmt.Errorf("agent service request failed: %w", err)
	}
	defer response.Body.Close()

	responseBody, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return Response{}, fmt.Errorf("read agent response: %w", err)
	}
	return Response{
		StatusCode:  response.StatusCode,
		ContentType: response.Header.Get("Content-Type"),
		Body:        responseBody,
	}, nil
}

// ChatStream opens the streaming chat endpoint and hands back the live body.
//
// The caller owns the returned ReadCloser and must close it, which is also
// what releases the upstream connection when a client disconnects early.  The
// 1 MiB limit the blocking Chat applies is deliberately absent here: it exists
// to bound a body that is read all at once, and applying it to a stream would
// silently truncate a long turn.
func (c *Client) ChatStream(ctx context.Context, request ChatRequest) (io.ReadCloser, error) {
	httpRequest, err := c.newChatRequest(ctx, "/internal/agent/chat/stream", request)
	if err != nil {
		return nil, err
	}
	httpRequest.Header.Set("Accept", "text/event-stream")

	response, err := c.streamClient().Do(httpRequest)
	if err != nil {
		return nil, fmt.Errorf("agent service stream failed: %w", err)
	}

	if response.StatusCode < 200 || response.StatusCode >= 300 {
		raw, readErr := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		response.Body.Close()
		if readErr != nil {
			return nil, fmt.Errorf("read agent stream error body: %w", readErr)
		}
		return nil, &StreamError{
			StatusCode:  response.StatusCode,
			ContentType: response.Header.Get("Content-Type"),
			Body:        raw,
		}
	}
	return response.Body, nil
}

// Session reads one session's current business state.
//
// It is a GET, and it never runs an Agent turn: a refreshed page needs to know
// what already happened, not to make something happen.  Go stays a transport
// here for the same reason it does for chat: the payload is the Python
// service's contract.
func (c *Client) Session(ctx context.Context, sessionID string, requestID string) (Response, error) {
	trimmed := strings.TrimSpace(sessionID)
	if trimmed == "" {
		return Response{}, fmt.Errorf("session id must not be blank")
	}

	httpRequest, err := http.NewRequestWithContext(
		ctx,
		http.MethodGet,
		c.BaseURL+"/internal/agent/session/"+url.PathEscape(trimmed),
		nil,
	)
	if err != nil {
		return Response{}, fmt.Errorf("create agent session request: %w", err)
	}
	httpRequest.Header.Set("Accept", "application/json")
	httpRequest.Header.Set("X-Request-ID", requestID)

	response, err := c.HTTPClient.Do(httpRequest)
	if err != nil {
		return Response{}, fmt.Errorf("agent session request failed: %w", err)
	}
	defer response.Body.Close()

	responseBody, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return Response{}, fmt.Errorf("read agent session response: %w", err)
	}
	return Response{
		StatusCode:  response.StatusCode,
		ContentType: response.Header.Get("Content-Type"),
		Body:        responseBody,
	}, nil
}

func (c *Client) newChatRequest(ctx context.Context, path string, request ChatRequest) (*http.Request, error) {
	body, err := json.Marshal(request)
	if err != nil {
		return nil, fmt.Errorf("marshal agent request: %w", err)
	}

	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create agent request: %w", err)
	}
	httpRequest.Header.Set("Content-Type", "application/json")
	httpRequest.Header.Set("X-Request-ID", request.RequestID)
	return httpRequest, nil
}

// streamClient drops any whole-response timeout the caller configured.
//
// http.Client.Timeout covers reading the entire body, so a client tuned for
// the blocking endpoint would cut every stream at that deadline no matter how
// healthy the turn is.  Everything else about the caller's client is kept.
func (c *Client) streamClient() *http.Client {
	if c.HTTPClient.Timeout == 0 {
		return c.HTTPClient
	}
	derived := *c.HTTPClient
	derived.Timeout = 0
	return &derived
}
