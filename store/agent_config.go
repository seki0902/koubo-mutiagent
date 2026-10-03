package store

import (
	"os"
	"strings"

	"koubo-video-tool/crypto"
)

// AgentModelConfig is one model role for the Python Agent service.
//
// The field names match the environment variables the service also accepts
// (SUPERVISOR_MODEL / SCRIPT_MODEL and their _PROVIDER / _API_BASE_URL / _API_KEY
// neighbours), so one setting has one meaning in both places.
type AgentModelConfig struct {
	Provider   string `json:"provider"`
	Model      string `json:"model"`
	APIBaseURL string `json:"api_base_url"`
	APIKey     string `json:"api_key"`
}

// AgentConfig is the Agent-service half of the application settings.
//
// It lives in the same config.json as the Chanjing and LLM settings on
// purpose: the user fills one file, and every secret is protected by the same
// machine-bound encryption.
type AgentConfig struct {
	Supervisor AgentModelConfig `json:"supervisor"`
	Script     AgentModelConfig `json:"script"`
}

// LoadAgentConfig reads the Agent settings with secrets decrypted.
//
// A missing file yields an empty configuration rather than an error: an
// installation that never opened the Agent panel is normal, and the caller
// decides what to fall back to.
func LoadAgentConfig(path string) (AgentConfig, error) {
	var cfg Config
	if err := read(path, &cfg); err != nil {
		if os.IsNotExist(err) {
			return AgentConfig{}, nil
		}
		return AgentConfig{}, err
	}
	return cfg.agentDecrypted(), nil
}

// SaveAgentConfig stores the Agent settings, encrypting both keys.
//
// It preserves every other section of the file, so saving the Agent panel
// cannot wipe the Chanjing or search settings the main page owns.  The file is
// read in its encrypted form and written straight back, so the other sections'
// secrets are never decrypted and re-encrypted on the way through.
func SaveAgentConfig(path string, agent AgentConfig) error {
	cfg, err := LoadConfigFile(path)
	if err != nil {
		return err
	}
	cfg.Agent = agent.encrypted()
	return write(path, cfg)
}

// LoadConfigFile reads the whole settings file in its stored (encrypted) form.
//
// Used by callers that rewrite one section and must not disturb the others.
func LoadConfigFile(path string) (Config, error) {
	var cfg Config
	if err := read(path, &cfg); err != nil {
		if os.IsNotExist(err) {
			return Config{}, nil
		}
		return Config{}, err
	}
	return cfg, nil
}

// decrypted returns a copy with every stored secret in the clear.
func (c Config) decrypted() Config {
	c.Chanjing.SecretKey = decryptOrKeep(c.Chanjing.SecretKey)
	c.LLM.APIKey = decryptOrKeep(c.LLM.APIKey)
	c.Search.APIKey = decryptOrKeep(c.Search.APIKey)
	c.Agent = c.agentDecrypted()
	return c
}

// encrypted returns a copy with every secret sealed for storage.
func (c Config) encrypted() Config {
	c.Chanjing.SecretKey = encryptOrKeep(c.Chanjing.SecretKey)
	c.LLM.APIKey = encryptOrKeep(c.LLM.APIKey)
	c.Search.APIKey = encryptOrKeep(c.Search.APIKey)
	c.Agent = c.Agent.encrypted()
	return c
}

// agentDecrypted returns the Agent section with both keys in the clear.
func (c Config) agentDecrypted() AgentConfig {
	agent := c.Agent
	agent.Supervisor.APIKey = decryptOrKeep(agent.Supervisor.APIKey)
	agent.Script.APIKey = decryptOrKeep(agent.Script.APIKey)
	return agent
}

// encrypted returns a copy with both Agent keys sealed for storage.
func (a AgentConfig) encrypted() AgentConfig {
	a.Supervisor.APIKey = encryptOrKeep(a.Supervisor.APIKey)
	a.Script.APIKey = encryptOrKeep(a.Script.APIKey)
	return a
}

func decryptOrKeep(value string) string {
	if strings.TrimSpace(value) == "" {
		return ""
	}
	plain, err := crypto.Decrypt(value)
	if err != nil {
		return ""
	}
	return plain
}

func encryptOrKeep(value string) string {
	if strings.TrimSpace(value) == "" {
		return ""
	}
	// A value that is already sealed is kept as it is. Without this, the merge
	// paths that carry a stored (still encrypted) secret forward would seal it a
	// second time and destroy it — which is exactly what a settings file that
	// cannot be decrypted on this machine ends up holding.
	if strings.HasPrefix(strings.TrimSpace(value), "ENC:") {
		return strings.TrimSpace(value)
	}
	encrypted, err := crypto.Encrypt(value)
	if err != nil {
		return ""
	}
	return encrypted
}
