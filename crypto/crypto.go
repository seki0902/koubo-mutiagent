package crypto

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"os"
	"strings"
)

const prefix = "ENC:"

var ErrDecryptFailed = errors.New("crypto: decrypt failed")

// deriveKey 从主机名派生 AES-256 密钥，绑定到当前机器
func deriveKey() []byte {
	hostname, _ := os.Hostname()
	h := sha256.Sum256([]byte(hostname + ":koubo-video-tool:key-v1"))
	return h[:]
}

// Encrypt 加密明文，返回 "ENC:" + base64(nonce||ciphertext)
func Encrypt(plaintext string) (string, error) {
	if plaintext == "" {
		return "", nil
	}
	key := deriveKey()
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	ciphertext := gcm.Seal(nonce, nonce, []byte(plaintext), nil)
	return prefix + base64.StdEncoding.EncodeToString(ciphertext), nil
}

// Decrypt 解密。不以 "ENC:" 开头则视为旧明文，直接返回（平滑迁移）
func Decrypt(encoded string) (string, error) {
	if encoded == "" {
		return "", nil
	}
	if !strings.HasPrefix(encoded, prefix) {
		// 旧明文，直接返回（下次保存时会自动加密）
		return encoded, nil
	}
	key := deriveKey()
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	data, err := base64.StdEncoding.DecodeString(encoded[len(prefix):])
	if err != nil {
		return "", ErrDecryptFailed
	}
	nonceSize := gcm.NonceSize()
	if len(data) < nonceSize {
		return "", ErrDecryptFailed
	}
	plaintext, err := gcm.Open(nil, data[:nonceSize], data[nonceSize:], nil)
	if err != nil {
		return "", ErrDecryptFailed
	}
	return string(plaintext), nil
}
