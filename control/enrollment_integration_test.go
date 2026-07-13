//go:build integration

package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

type enrollmentHTTPDevice struct {
	private   *ecdsa.PrivateKey
	publicKey string
}

func newEnrollmentHTTPDevice(t *testing.T) enrollmentHTTPDevice {
	t.Helper()
	private, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	raw := elliptic.Marshal(elliptic.P256(), private.X, private.Y)
	return enrollmentHTTPDevice{
		private:   private,
		publicKey: base64.RawURLEncoding.EncodeToString(raw),
	}
}

func enrollmentHTTPProof(
	t *testing.T,
	private *ecdsa.PrivateKey,
	code, audience string,
	accountID uuid.UUID,
	controlOrigin, requestID string,
) string {
	t.Helper()
	digest := sha256.Sum256(enrollmentExchangeTranscript(code, audience, accountID, controlOrigin, requestID))
	signature, err := ecdsa.SignASN1(rand.Reader, private, digest[:])
	if err != nil {
		t.Fatal(err)
	}
	return base64.RawURLEncoding.EncodeToString(signature)
}

func issueEnrollmentCodeHTTP(
	t *testing.T,
	account supplierTestAccount,
	device enrollmentHTTPDevice,
	rotateFrom *uuid.UUID,
) EnrollmentCodeIssueResult {
	t.Helper()
	body := EnrollmentCodeIssueInput{
		Audience:               workerEnrollmentAudience,
		DeviceKeyAlgorithm:     workerEnrollmentKeyAlgorithm,
		DevicePublicKey:        device.publicKey,
		Label:                  "Kitchen Mac",
		RotateFromCredentialID: rotateFrom,
	}
	code, out := req(t, http.MethodPost, "/v1/supplier/enrollment-codes", body, jsonCT(), account.auth)
	if code != http.StatusCreated {
		t.Fatalf("issue enrollment code: want 201, got %d: %s", code, out)
	}
	var result EnrollmentCodeIssueResult
	if err := json.Unmarshal(out, &result); err != nil {
		t.Fatalf("decode issued enrollment code: %v (%s)", err, out)
	}
	if result.Version != workerEnrollmentProtocolVersion || result.EnrollmentCodeID == uuid.Nil ||
		!validEnrollmentCode(result.EnrollmentCode) || result.ControlOrigin == "" || result.RequestID == "" {
		t.Fatalf("invalid enrollment-code response: %+v", result)
	}
	return result
}

func decodeEnrollmentApprovalBundleHTTP(t *testing.T, encoded string) enrollmentApprovalBundlePayload {
	t.Helper()
	if !strings.HasPrefix(encoded, workerEnrollmentBundlePrefix) {
		t.Fatalf("approval bundle missing %s prefix", workerEnrollmentBundlePrefix)
	}
	payloadText := strings.TrimPrefix(encoded, workerEnrollmentBundlePrefix)
	payload, err := base64.RawURLEncoding.DecodeString(payloadText)
	if err != nil || base64.RawURLEncoding.EncodeToString(payload) != payloadText {
		t.Fatalf("approval bundle is not canonical base64url: %v", err)
	}
	var decoded enrollmentApprovalBundlePayload
	if err := decodeExactEnrollmentObject(payload, &decoded,
		"v", "control_origin", "account_id", "audience", "enrollment_code", "request_id", "device_fingerprint"); err != nil {
		t.Fatalf("approval bundle schema: %v (%s)", err, payload)
	}
	return decoded
}

func approveEnrollmentRequestHTTP(
	t *testing.T,
	account supplierTestAccount,
	device enrollmentHTTPDevice,
	label string,
) (EnrollmentApprovalResult, enrollmentApprovalBundlePayload, http.Header) {
	t.Helper()
	testKey := enrollmentTestKey{private: device.private, encoded: device.publicKey}
	deviceRequest := encodeEnrollmentDeviceRequestTest(t,
		enrollmentDeviceRequestPayload(t, testKey, itHTTP.URL))
	result := enrollmentRawRequest(http.MethodPost, "/v1/supplier/enrollment-approvals",
		EnrollmentApprovalInput{DeviceRequest: deviceRequest, Label: label}, jsonCT(), account.auth)
	if result.err != nil {
		t.Fatal(result.err)
	}
	if result.status != http.StatusCreated {
		t.Fatalf("approve enrollment request: want 201, got %d: %s", result.status, result.body)
	}
	var approval EnrollmentApprovalResult
	if err := json.Unmarshal(result.body, &approval); err != nil {
		t.Fatalf("decode enrollment approval: %v (%s)", err, result.body)
	}
	if approval.EnrollmentCodeID == uuid.Nil || approval.EnrollmentBundle == "" {
		t.Fatalf("incomplete enrollment approval: %+v", approval)
	}
	return approval, decodeEnrollmentApprovalBundleHTTP(t, approval.EnrollmentBundle), result.header
}

func enrollmentExchangeBody(
	t *testing.T,
	issued EnrollmentCodeIssueResult,
	device enrollmentHTTPDevice,
	audience string,
	accountID uuid.UUID,
) EnrollmentExchangeInput {
	t.Helper()
	return EnrollmentExchangeInput{
		Version:            issued.Version,
		EnrollmentCode:     issued.EnrollmentCode,
		ControlOrigin:      issued.ControlOrigin,
		RequestID:          issued.RequestID,
		Audience:           audience,
		AccountID:          accountID,
		DeviceKeyAlgorithm: workerEnrollmentKeyAlgorithm,
		DevicePublicKey:    device.publicKey,
		Proof: enrollmentHTTPProof(t, device.private, issued.EnrollmentCode, audience, accountID,
			issued.ControlOrigin, issued.RequestID),
	}
}

func exchangeEnrollmentHTTP(t *testing.T, body EnrollmentExchangeInput) (int, []byte) {
	t.Helper()
	return req(t, http.MethodPost, "/v1/worker/enrollment/exchange", body, jsonCT())
}

type enrollmentRawHTTPResult struct {
	status int
	body   []byte
	header http.Header
	err    error
}

func enrollmentRawRequest(method, path string, body any, headers ...hdr) enrollmentRawHTTPResult {
	blob, err := json.Marshal(body)
	if err != nil {
		return enrollmentRawHTTPResult{err: err}
	}
	r, err := http.NewRequest(method, itHTTP.URL+path, strings.NewReader(string(blob)))
	if err != nil {
		return enrollmentRawHTTPResult{err: err}
	}
	for _, h := range headers {
		r.Header.Set(h.k, h.v)
	}
	resp, err := http.DefaultClient.Do(r)
	if err != nil {
		return enrollmentRawHTTPResult{err: err}
	}
	defer resp.Body.Close()
	out, err := io.ReadAll(resp.Body)
	return enrollmentRawHTTPResult{status: resp.StatusCode, body: out, header: resp.Header.Clone(), err: err}
}

func mustExchangeEnrollmentHTTP(t *testing.T, body EnrollmentExchangeInput) EnrollmentExchangeResult {
	t.Helper()
	code, out := exchangeEnrollmentHTTP(t, body)
	if code != http.StatusCreated {
		t.Fatalf("exchange enrollment code: want 201, got %d: %s", code, out)
	}
	var result EnrollmentExchangeResult
	if err := json.Unmarshal(out, &result); err != nil {
		t.Fatalf("decode enrollment exchange: %v (%s)", err, out)
	}
	if result.CredentialID == uuid.Nil || result.WorkerID == uuid.Nil ||
		!strings.HasPrefix(result.WorkerToken, "cxw_") {
		t.Fatalf("invalid enrollment exchange response: %+v", result)
	}
	return result
}

func TestWorkerEnrollmentAuthenticatedApprovalAdapter(t *testing.T) {
	reset(t)
	t.Setenv(publicControlOriginEnv, "")
	ctx := context.Background()
	account := newSupplierTestAccount(t, "enrollment-approval-owner")
	device := newEnrollmentHTTPDevice(t)
	testKey := enrollmentTestKey{private: device.private, encoded: device.publicKey}
	deviceRequest := encodeEnrollmentDeviceRequestTest(t,
		enrollmentDeviceRequestPayload(t, testKey, itHTTP.URL))

	if code, _ := req(t, http.MethodPost, "/v1/supplier/enrollment-approvals",
		EnrollmentApprovalInput{DeviceRequest: deviceRequest}, jsonCT()); code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated approval: want 401, got %d", code)
	}

	approval, bundle, headers := approveEnrollmentRequestHTTP(t, account, device, "Office Studio")
	if !strings.Contains(strings.ToLower(headers.Get("Cache-Control")), "no-store") ||
		!strings.EqualFold(headers.Get("Pragma"), "no-cache") {
		t.Fatalf("secret approval response is cacheable: %v", headers)
	}
	remaining := time.Until(approval.ExpiresAt)
	if remaining < 9*time.Minute || remaining > 11*time.Minute {
		t.Fatalf("approval TTL outside ten-minute window: %s", remaining)
	}
	_, _, wantFingerprint, err := parseP256EnrollmentPublicKey(device.publicKey)
	if err != nil {
		t.Fatal(err)
	}
	wantRequestID := enrollmentRequestID(elliptic.Marshal(elliptic.P256(), device.private.X, device.private.Y))
	if bundle.Version != workerEnrollmentProtocolVersion || bundle.ControlOrigin != itHTTP.URL || bundle.AccountID != account.buyerID ||
		bundle.Audience != workerEnrollmentAudience || !validEnrollmentCode(bundle.EnrollmentCode) ||
		bundle.RequestID != wantRequestID || bundle.DeviceFingerprint != wantFingerprint {
		t.Fatalf("approval bundle lost request/account binding: %+v", bundle)
	}

	var rowBuyer, rowSupplier, supplierOwner uuid.UUID
	var storedHash, label, storedOrigin, storedRequestID string
	var storedVersion int
	if err := itPool.QueryRow(ctx, `
		SELECT c.buyer_id,c.supplier_id,sp.owner_buyer_id,c.code_hash,COALESCE(c.label,''),
		       c.protocol_version,c.control_origin,c.request_id
		  FROM worker_enrollment_codes c JOIN suppliers sp ON sp.id=c.supplier_id
		 WHERE c.id=$1`, approval.EnrollmentCodeID).
		Scan(&rowBuyer, &rowSupplier, &supplierOwner, &storedHash, &label,
			&storedVersion, &storedOrigin, &storedRequestID); err != nil {
		t.Fatalf("load approved enrollment row: %v", err)
	}
	if rowBuyer != account.buyerID || supplierOwner != account.buyerID || rowSupplier == uuid.Nil ||
		storedHash != hashKey(bundle.EnrollmentCode) || storedHash == bundle.EnrollmentCode || label != "Office Studio" ||
		storedVersion != workerEnrollmentProtocolVersion || storedOrigin != bundle.ControlOrigin ||
		storedRequestID != bundle.RequestID {
		t.Fatalf("approval row not owned/hashed as authenticated account: buyer=%s supplier=%s owner=%s hash=%q label=%q",
			rowBuyer, rowSupplier, supplierOwner, storedHash, label)
	}

	exchanged := mustExchangeEnrollmentHTTP(t, EnrollmentExchangeInput{
		Version:            bundle.Version,
		EnrollmentCode:     bundle.EnrollmentCode,
		ControlOrigin:      bundle.ControlOrigin,
		RequestID:          bundle.RequestID,
		Audience:           bundle.Audience,
		AccountID:          bundle.AccountID,
		DeviceKeyAlgorithm: workerEnrollmentKeyAlgorithm,
		DevicePublicKey:    device.publicKey,
		Proof: enrollmentHTTPProof(t, device.private, bundle.EnrollmentCode,
			bundle.Audience, bundle.AccountID, bundle.ControlOrigin, bundle.RequestID),
	})
	if exchanged.SupplierID != rowSupplier || exchanged.DeviceFingerprint != wantFingerprint {
		t.Fatalf("adapter bundle did not exchange into its bound supplier/device: %+v", exchanged)
	}

	// A different authenticated account cannot select the first buyer/supplier by
	// smuggling identity fields. Unknown fields fail before code issuance; a clean
	// approval for a different key is owned only by the presenting account.
	other := newSupplierTestAccount(t, "enrollment-approval-other")
	otherDevice := newEnrollmentHTTPDevice(t)
	otherKey := enrollmentTestKey{private: otherDevice.private, encoded: otherDevice.publicKey}
	otherRequest := encodeEnrollmentDeviceRequestTest(t,
		enrollmentDeviceRequestPayload(t, otherKey, itHTTP.URL))
	malicious := map[string]any{
		"device_request": otherRequest,
		"account_id":     account.buyerID,
		"supplier_id":    rowSupplier,
	}
	if code, out := req(t, http.MethodPost, "/v1/supplier/enrollment-approvals",
		malicious, jsonCT(), other.auth); code != http.StatusBadRequest {
		t.Fatalf("caller-selected ownership accepted: want 400, got %d: %s", code, out)
	}
	var leakedRows int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM worker_enrollment_codes WHERE buyer_id=$1`, other.buyerID).Scan(&leakedRows); err != nil {
		t.Fatal(err)
	}
	if leakedRows != 0 {
		t.Fatalf("rejected caller-selected ownership still issued %d code(s)", leakedRows)
	}
	_, otherBundle, _ := approveEnrollmentRequestHTTP(t, other, otherDevice, "Other Mac")
	if otherBundle.AccountID != other.buyerID || otherBundle.AccountID == account.buyerID {
		t.Fatalf("approval account came from caller rather than auth: %+v", otherBundle)
	}
}

func TestWorkerEnrollmentApprovalRejectsMalformedTamperedWrongOriginAudienceAndExtraFields(t *testing.T) {
	reset(t)
	t.Setenv(publicControlOriginEnv, "")
	ctx := context.Background()
	account := newSupplierTestAccount(t, "enrollment-approval-invalid")
	device := newEnrollmentHTTPDevice(t)
	testKey := enrollmentTestKey{private: device.private, encoded: device.publicKey}

	mutated := func(field string, value any) string {
		payload := enrollmentDeviceRequestPayload(t, testKey, itHTTP.URL)
		payload[field] = value
		return encodeEnrollmentDeviceRequestTest(t, payload)
	}
	extra := enrollmentDeviceRequestPayload(t, testKey, itHTTP.URL)
	extra["account_id"] = account.buyerID
	cases := map[string]string{
		"malformed":           "cxer1_not+base64",
		"tampered request id": mutated("request_id", "AAAAAAAAAAAAAAAAAAAAAA"),
		"wrong origin":        mutated("control_origin", "http://localhost:1"),
		"wrong audience":      mutated("audience", "cx-other-agent-v1"),
		"extra field":         encodeEnrollmentDeviceRequestTest(t, extra),
	}
	for name, request := range cases {
		t.Run(name, func(t *testing.T) {
			result := enrollmentRawRequest(http.MethodPost, "/v1/supplier/enrollment-approvals",
				EnrollmentApprovalInput{DeviceRequest: request}, jsonCT(), account.auth)
			if result.err != nil {
				t.Fatal(result.err)
			}
			if result.status != http.StatusBadRequest {
				t.Fatalf("want 400, got %d: %s", result.status, result.body)
			}
			if !strings.Contains(strings.ToLower(result.header.Get("Cache-Control")), "no-store") {
				t.Fatalf("enrollment error response is cacheable: %v", result.header)
			}
		})
	}
	var rows int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM worker_enrollment_codes WHERE buyer_id=$1`, account.buyerID).Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 0 {
		t.Fatalf("invalid approval requests issued %d code(s)", rows)
	}
}

func TestWorkerEnrollmentApprovalConfiguredOriginIgnoresSpoofedHostAndForwardedProto(t *testing.T) {
	reset(t)
	configuredOrigin := "https://control.example.test"
	t.Setenv(publicControlOriginEnv, configuredOrigin)
	account := newSupplierTestAccount(t, "enrollment-approval-configured-origin")
	device := newEnrollmentHTTPDevice(t)
	testKey := enrollmentTestKey{private: device.private, encoded: device.publicKey}
	deviceRequest := encodeEnrollmentDeviceRequestTest(t,
		enrollmentDeviceRequestPayload(t, testKey, configuredOrigin))
	body, err := json.Marshal(EnrollmentApprovalInput{DeviceRequest: deviceRequest, Label: "Configured Mac"})
	if err != nil {
		t.Fatal(err)
	}
	r, err := http.NewRequest(http.MethodPost, itHTTP.URL+"/v1/supplier/enrollment-approvals", strings.NewReader(string(body)))
	if err != nil {
		t.Fatal(err)
	}
	r.Host = "attacker.invalid"
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("Authorization", account.auth.v)
	// This deliberately malformed proxy value would have controlled/rejected the
	// old origin derivation. With a configured origin it is inert.
	r.Header.Set("X-Forwarded-Proto", "https://attacker.invalid")
	resp, err := http.DefaultClient.Do(r)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	responseBody, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("configured-origin approval: want 201, got %d: %s", resp.StatusCode, responseBody)
	}
	var approval EnrollmentApprovalResult
	if err := json.Unmarshal(responseBody, &approval); err != nil {
		t.Fatalf("decode configured-origin approval: %v (%s)", err, responseBody)
	}
	bundle := decodeEnrollmentApprovalBundleHTTP(t, approval.EnrollmentBundle)
	if bundle.ControlOrigin != configuredOrigin || bundle.AccountID != account.buyerID {
		t.Fatalf("spoofed headers changed configured approval binding: %+v", bundle)
	}
}

func TestWorkerEnrollmentOneTimeProofedLifecycle(t *testing.T) {
	reset(t)
	ctx := context.Background()
	account := newSupplierTestAccount(t, "enrollment-lifecycle")
	other := newSupplierTestAccount(t, "enrollment-other")
	device := newEnrollmentHTTPDevice(t)
	issued := issueEnrollmentCodeHTTP(t, account, device, nil)

	if issued.AccountID != account.buyerID || issued.Audience != workerEnrollmentAudience || issued.Rotation {
		t.Fatalf("issued code not bound to authenticated account/audience: %+v", issued)
	}
	remaining := time.Until(issued.ExpiresAt)
	if remaining < 9*time.Minute || remaining > 11*time.Minute {
		t.Fatalf("enrollment code TTL outside ten-minute window: %s", remaining)
	}

	exchangeBody := enrollmentExchangeBody(t, issued, device, workerEnrollmentAudience, account.buyerID)
	exchanged := mustExchangeEnrollmentHTTP(t, exchangeBody)
	if exchanged.SupplierID != issued.SupplierID || exchanged.DeviceFingerprint != issued.DeviceFingerprint ||
		exchanged.CredentialVersion != 1 || exchanged.Rotated {
		t.Fatalf("wrong first credential binding: %+v", exchanged)
	}
	auth, err := itStore.LookupWorkerToken(ctx, exchanged.WorkerToken)
	if err != nil {
		t.Fatalf("lookup exchanged worker token: %v", err)
	}
	if auth.WorkerID != exchanged.WorkerID || auth.SupplierID != issued.SupplierID ||
		auth.CredentialID != exchanged.CredentialID || !auth.EnrollmentDeviceBound ||
		auth.DeviceFingerprint != issued.DeviceFingerprint || auth.CredentialVersion != 1 {
		t.Fatalf("stored worker credential lost binding: %+v", auth)
	}

	// Raw code/token material must never be retained in their hash columns.
	var rawCodeRows, rawTokenRows, hashedCodeRows, hashedTokenRows int
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_enrollment_codes WHERE code_hash=$1`, issued.EnrollmentCode).Scan(&rawCodeRows); err != nil {
		t.Fatal(err)
	}
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_tokens WHERE token_hash=$1`, exchanged.WorkerToken).Scan(&rawTokenRows); err != nil {
		t.Fatal(err)
	}
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_enrollment_codes WHERE code_hash=$1`, hashKey(issued.EnrollmentCode)).Scan(&hashedCodeRows); err != nil {
		t.Fatal(err)
	}
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_tokens WHERE token_hash=$1`, hashKey(exchanged.WorkerToken)).Scan(&hashedTokenRows); err != nil {
		t.Fatal(err)
	}
	if rawCodeRows != 0 || rawTokenRows != 0 || hashedCodeRows != 1 || hashedTokenRows != 1 {
		t.Fatalf("secret-at-rest invariant failed: raw code=%d raw token=%d hash code=%d hash token=%d",
			rawCodeRows, rawTokenRows, hashedCodeRows, hashedTokenRows)
	}

	code, out := req(t, http.MethodGet, "/v1/worker/connect/status", nil,
		hdr{"X-Worker-Token", exchanged.WorkerToken})
	if code != http.StatusOK {
		t.Fatalf("connect status with exchanged token: want 200, got %d: %s", code, out)
	}
	var connectStatus struct {
		CredentialID          uuid.UUID `json:"credential_id"`
		EnrollmentDeviceBound bool      `json:"enrollment_device_bound"`
		DeviceFingerprint     string    `json:"device_fingerprint"`
		CredentialVersion     int       `json:"credential_version"`
	}
	if err := json.Unmarshal(out, &connectStatus); err != nil {
		t.Fatal(err)
	}
	if connectStatus.CredentialID != exchanged.CredentialID || !connectStatus.EnrollmentDeviceBound ||
		connectStatus.DeviceFingerprint != issued.DeviceFingerprint || connectStatus.CredentialVersion != 1 {
		t.Fatalf("connect status omitted credential binding: %+v", connectStatus)
	}

	// A committed code is single-use. Replaying it produces exactly the same public
	// response as every other rejected exchange and creates no second credential.
	code, replayBody := exchangeEnrollmentHTTP(t, exchangeBody)
	if code != http.StatusUnauthorized || !strings.Contains(string(replayBody), "enrollment exchange rejected") {
		t.Fatalf("replay: want generic 401, got %d: %s", code, replayBody)
	}
	var workerTokenCount int
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_tokens WHERE supplier_id=$1`, issued.SupplierID).Scan(&workerTokenCount); err != nil {
		t.Fatal(err)
	}
	if workerTokenCount != 1 {
		t.Fatalf("replay minted credentials: want 1 token, got %d", workerTokenCount)
	}
	for i := 0; i < 5; i++ {
		if code, _ := exchangeEnrollmentHTTP(t, exchangeBody); code != http.StatusUnauthorized {
			t.Fatalf("terminal replay %d: want 401, got %d", i, code)
		}
	}
	var replayEvents int
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FROM worker_credential_audit
		 WHERE enrollment_code_id=$1 AND event_type='exchange_rejected' AND reason='replay'`,
		issued.EnrollmentCodeID).Scan(&replayEvents); err != nil {
		t.Fatal(err)
	}
	if replayEvents != 1 {
		t.Fatalf("terminal replay amplified audit rows: want 1, got %d", replayEvents)
	}

	code, out = req(t, http.MethodGet, "/v1/supplier/worker-credentials", nil, account.auth)
	if code != http.StatusOK || !strings.Contains(string(out), exchanged.CredentialID.String()) {
		t.Fatalf("owner credential list: got %d: %s", code, out)
	}
	code, out = req(t, http.MethodGet, "/v1/supplier/worker-credentials", nil, other.auth)
	if code != http.StatusOK || strings.Contains(string(out), exchanged.CredentialID.String()) {
		t.Fatalf("cross-account credential list leaked owner credential: %d: %s", code, out)
	}
	code, out = req(t, http.MethodDelete,
		"/v1/supplier/worker-credentials/"+exchanged.CredentialID.String(), nil, other.auth)
	if code != http.StatusNotFound {
		t.Fatalf("cross-account revoke: want 404, got %d: %s", code, out)
	}

	code, out = req(t, http.MethodGet, "/v1/supplier/credential-audit", nil, account.auth)
	if code != http.StatusOK || !strings.Contains(string(out), `"reason":"replay"`) ||
		!strings.Contains(string(out), `"event_type":"exchange_succeeded"`) {
		t.Fatalf("credential audit missing lifecycle/replay evidence: %d: %s", code, out)
	}

	code, out = req(t, http.MethodDelete,
		"/v1/supplier/worker-credentials/"+exchanged.CredentialID.String(), nil, account.auth)
	if code != http.StatusNoContent {
		t.Fatalf("owner revoke: want 204, got %d: %s", code, out)
	}
	if _, err := itStore.LookupWorkerToken(ctx, exchanged.WorkerToken); !errors.Is(err, errNotFound) {
		t.Fatalf("revoked token still authenticates: %v", err)
	}
}

func TestWorkerEnrollmentRejectsWrongBindingsExpiryRevocationAndAttemptFlood(t *testing.T) {
	reset(t)
	ctx := context.Background()
	owner := newSupplierTestAccount(t, "enrollment-reject-owner")
	other := newSupplierTestAccount(t, "enrollment-reject-other")
	genericBody := ""

	assertRejected := func(name string, body EnrollmentExchangeInput) {
		t.Helper()
		code, out := exchangeEnrollmentHTTP(t, body)
		if code != http.StatusUnauthorized {
			t.Fatalf("%s: want 401, got %d: %s", name, code, out)
		}
		if genericBody == "" {
			genericBody = string(out)
		} else if string(out) != genericBody {
			t.Fatalf("%s leaked rejection cause: first=%q got=%q", name, genericBody, out)
		}
	}

	device := newEnrollmentHTTPDevice(t)
	issued := issueEnrollmentCodeHTTP(t, owner, device, nil)
	wrongAccount := enrollmentExchangeBody(t, issued, device, workerEnrollmentAudience, other.buyerID)
	assertRejected("wrong account", wrongAccount)

	// Model the original credential-theft attack directly: a substituted bundle
	// points the Mac at an attacker-controlled HTTPS relay and the Mac signs that
	// altered origin. The v2 code row is bound to the trusted approval origin, so
	// forwarding this otherwise-valid device proof to the real server must fail.
	device = newEnrollmentHTTPDevice(t)
	issued = issueEnrollmentCodeHTTP(t, owner, device, nil)
	wrongOrigin := enrollmentExchangeBody(t, issued, device, workerEnrollmentAudience, owner.buyerID)
	wrongOrigin.ControlOrigin = "https://relay.example.test"
	wrongOrigin.Proof = enrollmentHTTPProof(t, device.private, issued.EnrollmentCode,
		workerEnrollmentAudience, owner.buyerID, wrongOrigin.ControlOrigin, wrongOrigin.RequestID)
	assertRejected("wrong control origin relay", wrongOrigin)

	device = newEnrollmentHTTPDevice(t)
	issued = issueEnrollmentCodeHTTP(t, owner, device, nil)
	wrongRequestID := enrollmentExchangeBody(t, issued, device, workerEnrollmentAudience, owner.buyerID)
	wrongRequestID.RequestID = strings.Repeat("A", 22)
	wrongRequestID.Proof = enrollmentHTTPProof(t, device.private, issued.EnrollmentCode,
		workerEnrollmentAudience, owner.buyerID, wrongRequestID.ControlOrigin, wrongRequestID.RequestID)
	assertRejected("wrong request id", wrongRequestID)

	device = newEnrollmentHTTPDevice(t)
	issued = issueEnrollmentCodeHTTP(t, owner, device, nil)
	wrongVersion := enrollmentExchangeBody(t, issued, device, workerEnrollmentAudience, owner.buyerID)
	wrongVersion.Version = 1
	assertRejected("legacy protocol version", wrongVersion)

	device = newEnrollmentHTTPDevice(t)
	issued = issueEnrollmentCodeHTTP(t, owner, device, nil)
	wrongAudience := enrollmentExchangeBody(t, issued, device, "cx-other-agent-v1", owner.buyerID)
	assertRejected("wrong audience", wrongAudience)

	device = newEnrollmentHTTPDevice(t)
	issued = issueEnrollmentCodeHTTP(t, owner, device, nil)
	wrongDevice := newEnrollmentHTTPDevice(t)
	assertRejected("wrong device", enrollmentExchangeBody(t, issued, wrongDevice, workerEnrollmentAudience, owner.buyerID))

	device = newEnrollmentHTTPDevice(t)
	issued = issueEnrollmentCodeHTTP(t, owner, device, nil)
	badProof := enrollmentExchangeBody(t, issued, device, workerEnrollmentAudience, owner.buyerID)
	badProof.Proof = enrollmentHTTPProof(t, device.private, newSecret(workerEnrollmentCodePrefix),
		workerEnrollmentAudience, owner.buyerID, issued.ControlOrigin, issued.RequestID)
	assertRejected("invalid proof", badProof)

	device = newEnrollmentHTTPDevice(t)
	issued = issueEnrollmentCodeHTTP(t, owner, device, nil)
	if _, err := itPool.Exec(ctx, `
		UPDATE worker_enrollment_codes
		   SET created_at=now()-interval '20 minutes',expires_at=now()-interval '10 minutes'
		 WHERE id=$1`, issued.EnrollmentCodeID); err != nil {
		t.Fatalf("expire enrollment code: %v", err)
	}
	assertRejected("expired", enrollmentExchangeBody(t, issued, device, workerEnrollmentAudience, owner.buyerID))

	device = newEnrollmentHTTPDevice(t)
	issued = issueEnrollmentCodeHTTP(t, owner, device, nil)
	code, out := req(t, http.MethodDelete,
		"/v1/supplier/enrollment-codes/"+issued.EnrollmentCodeID.String(), nil, owner.auth)
	if code != http.StatusNoContent {
		t.Fatalf("revoke enrollment code: want 204, got %d: %s", code, out)
	}
	assertRejected("revoked", enrollmentExchangeBody(t, issued, device, workerEnrollmentAudience, owner.buyerID))

	// Five known-code proof failures burn the code. A later valid proof cannot
	// recover it, limiting leaked-code probing and leaving an auditable trail.
	device = newEnrollmentHTTPDevice(t)
	issued = issueEnrollmentCodeHTTP(t, owner, device, nil)
	flood := enrollmentExchangeBody(t, issued, device, workerEnrollmentAudience, owner.buyerID)
	flood.Proof = enrollmentHTTPProof(t, device.private, newSecret(workerEnrollmentCodePrefix),
		workerEnrollmentAudience, owner.buyerID, issued.ControlOrigin, issued.RequestID)
	for i := 0; i < workerEnrollmentMaxFailedAttempts; i++ {
		assertRejected("attempt flood", flood)
	}
	var attempts int
	var revoked bool
	if err := itPool.QueryRow(ctx, `
		SELECT failed_attempts,revoked_at IS NOT NULL FROM worker_enrollment_codes WHERE id=$1`,
		issued.EnrollmentCodeID).Scan(&attempts, &revoked); err != nil {
		t.Fatal(err)
	}
	if attempts != workerEnrollmentMaxFailedAttempts || !revoked {
		t.Fatalf("attempt flood did not burn code: attempts=%d revoked=%v", attempts, revoked)
	}
	var autoRevokeEvents int
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FROM worker_credential_audit
		 WHERE enrollment_code_id=$1 AND event_type='code_revoked' AND reason='max_failed_attempts'`,
		issued.EnrollmentCodeID).Scan(&autoRevokeEvents); err != nil {
		t.Fatal(err)
	}
	if autoRevokeEvents != 1 {
		t.Fatalf("max-attempt lockout missing explicit audit event: got %d", autoRevokeEvents)
	}
	assertRejected("valid proof after flood", enrollmentExchangeBody(t, issued, device, workerEnrollmentAudience, owner.buyerID))

	wantReasons := []string{
		"wrong_account", "wrong_control_origin", "wrong_request_id", "wrong_protocol_version",
		"wrong_audience", "wrong_device", "invalid_device_proof", "expired", "revoked",
	}
	for _, reason := range wantReasons {
		var count int
		if err := itPool.QueryRow(ctx, `
			SELECT count(*) FROM worker_credential_audit WHERE buyer_id=$1 AND event_type='exchange_rejected' AND reason=$2`,
			owner.buyerID, reason).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count == 0 {
			t.Fatalf("missing audit reason %q", reason)
		}
	}
}

func TestWorkerEnrollmentRotationAndAppendOnlyAudit(t *testing.T) {
	reset(t)
	ctx := context.Background()
	owner := newSupplierTestAccount(t, "enrollment-rotate-owner")
	other := newSupplierTestAccount(t, "enrollment-rotate-other")
	firstDevice := newEnrollmentHTTPDevice(t)
	firstCode := issueEnrollmentCodeHTTP(t, owner, firstDevice, nil)
	first := mustExchangeEnrollmentHTTP(t,
		enrollmentExchangeBody(t, firstCode, firstDevice, workerEnrollmentAudience, owner.buyerID))

	// A different account cannot select the owner's credential as a rotation source.
	code, out := req(t, http.MethodPost, "/v1/supplier/enrollment-codes", EnrollmentCodeIssueInput{
		Audience:               workerEnrollmentAudience,
		DeviceKeyAlgorithm:     workerEnrollmentKeyAlgorithm,
		DevicePublicKey:        newEnrollmentHTTPDevice(t).publicKey,
		RotateFromCredentialID: &first.CredentialID,
	}, jsonCT(), other.auth)
	if code != http.StatusNotFound {
		t.Fatalf("cross-account rotation source: want 404, got %d: %s", code, out)
	}

	// Two pending rotations from different device keys still share one source
	// credential. Issuing the latter must supersede the former; fingerprint-only
	// revocation would leave both live and permit an unexpected winning device.
	supersededDevice := newEnrollmentHTTPDevice(t)
	supersededRotation := issueEnrollmentCodeHTTP(t, owner, supersededDevice, &first.CredentialID)
	secondDevice := newEnrollmentHTTPDevice(t)
	rotationCode := issueEnrollmentCodeHTTP(t, owner, secondDevice, &first.CredentialID)
	if !rotationCode.Rotation {
		t.Fatal("rotation code response did not identify rotation")
	}
	if code, _ := exchangeEnrollmentHTTP(t,
		enrollmentExchangeBody(t, supersededRotation, supersededDevice, workerEnrollmentAudience, owner.buyerID)); code != http.StatusUnauthorized {
		t.Fatalf("cross-device superseded rotation: want 401, got %d", code)
	}
	second := mustExchangeEnrollmentHTTP(t,
		enrollmentExchangeBody(t, rotationCode, secondDevice, workerEnrollmentAudience, owner.buyerID))
	if !second.Rotated || second.WorkerID != first.WorkerID || second.CredentialVersion != 2 ||
		second.CredentialID == first.CredentialID {
		t.Fatalf("rotation did not preserve worker/increment credential: first=%+v second=%+v", first, second)
	}
	if _, err := itStore.LookupWorkerToken(ctx, first.WorkerToken); !errors.Is(err, errNotFound) {
		t.Fatalf("rotation did not revoke old token: %v", err)
	}
	secondAuth, err := itStore.LookupWorkerToken(ctx, second.WorkerToken)
	if err != nil || secondAuth.CredentialID != second.CredentialID || secondAuth.CredentialVersion != 2 {
		t.Fatalf("rotated credential does not authenticate with version 2: %+v, %v", secondAuth, err)
	}

	var oldRevoked bool
	var oldReason string
	var rotatedFrom *uuid.UUID
	if err := itPool.QueryRow(ctx, `SELECT revoked,COALESCE(revocation_reason,'') FROM worker_tokens WHERE credential_id=$1`,
		first.CredentialID).Scan(&oldRevoked, &oldReason); err != nil {
		t.Fatal(err)
	}
	if err := itPool.QueryRow(ctx, `SELECT rotated_from_credential_id FROM worker_tokens WHERE credential_id=$1`,
		second.CredentialID).Scan(&rotatedFrom); err != nil {
		t.Fatal(err)
	}
	if !oldRevoked || oldReason != "rotated" || rotatedFrom == nil || *rotatedFrom != first.CredentialID {
		t.Fatalf("rotation chain not persisted: revoked=%v reason=%q from=%v", oldRevoked, oldReason, rotatedFrom)
	}

	var auditID uuid.UUID
	if err := itPool.QueryRow(ctx, `
		SELECT id FROM worker_credential_audit
		 WHERE buyer_id=$1 AND event_type='credential_rotated' AND credential_id=$2`,
		owner.buyerID, second.CredentialID).Scan(&auditID); err != nil {
		t.Fatalf("rotation audit missing: %v", err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE worker_credential_audit SET reason='rewritten' WHERE id=$1`, auditID); err == nil {
		t.Fatal("append-only credential audit accepted UPDATE")
	}
	if _, err := itPool.Exec(ctx, `DELETE FROM worker_credential_audit WHERE id=$1`, auditID); err == nil {
		t.Fatal("append-only credential audit accepted DELETE")
	}
}

func TestWorkerEnrollmentConcurrentExchangeIsExactlyOnce(t *testing.T) {
	reset(t)
	ctx := context.Background()
	account := newSupplierTestAccount(t, "enrollment-concurrent")
	device := newEnrollmentHTTPDevice(t)
	issued := issueEnrollmentCodeHTTP(t, account, device, nil)
	body := enrollmentExchangeBody(t, issued, device, workerEnrollmentAudience, account.buyerID)

	start := make(chan struct{})
	results := make(chan enrollmentRawHTTPResult, 2)
	for i := 0; i < 2; i++ {
		go func() {
			<-start
			results <- enrollmentRawRequest(http.MethodPost, "/v1/worker/enrollment/exchange", body, jsonCT())
		}()
	}
	close(start)
	statusCounts := map[int]int{}
	for i := 0; i < 2; i++ {
		result := <-results
		if result.err != nil {
			t.Fatal(result.err)
		}
		statusCounts[result.status]++
		if result.header.Get("Cache-Control") != "no-store" {
			t.Fatalf("secret exchange response is cacheable: %q", result.header.Get("Cache-Control"))
		}
	}
	if statusCounts[http.StatusCreated] != 1 || statusCounts[http.StatusUnauthorized] != 1 {
		t.Fatalf("concurrent exchange must be one success/one rejection, got %+v", statusCounts)
	}
	var tokenCount int
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_tokens WHERE supplier_id=$1`, issued.SupplierID).Scan(&tokenCount); err != nil {
		t.Fatal(err)
	}
	if tokenCount != 1 {
		t.Fatalf("concurrent exchange minted %d credentials, want 1", tokenCount)
	}
}

func TestWorkerEnrollmentExpiryIsRecheckedAfterLockWait(t *testing.T) {
	reset(t)
	ctx := context.Background()
	account := newSupplierTestAccount(t, "enrollment-expiry-lock")
	device := newEnrollmentHTTPDevice(t)
	issued := issueEnrollmentCodeHTTP(t, account, device, nil)
	if _, err := itPool.Exec(ctx, `
		UPDATE worker_enrollment_codes SET expires_at=clock_timestamp()+interval '300 milliseconds'
		 WHERE id=$1`, issued.EnrollmentCodeID); err != nil {
		t.Fatal(err)
	}
	lockTx, err := itPool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var locked uuid.UUID
	if err := lockTx.QueryRow(ctx, `SELECT id FROM worker_enrollment_codes WHERE id=$1 FOR UPDATE`,
		issued.EnrollmentCodeID).Scan(&locked); err != nil {
		lockTx.Rollback(ctx)
		t.Fatal(err)
	}
	resultCh := make(chan enrollmentRawHTTPResult, 1)
	body := enrollmentExchangeBody(t, issued, device, workerEnrollmentAudience, account.buyerID)
	go func() {
		resultCh <- enrollmentRawRequest(http.MethodPost, "/v1/worker/enrollment/exchange", body, jsonCT())
	}()
	time.Sleep(550 * time.Millisecond)
	if err := lockTx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	result := <-resultCh
	if result.err != nil || result.status != http.StatusUnauthorized {
		t.Fatalf("exchange waiting past expiry must reject: status=%d err=%v body=%s", result.status, result.err, result.body)
	}
	var tokens int
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_tokens WHERE supplier_id=$1`, issued.SupplierID).Scan(&tokens); err != nil {
		t.Fatal(err)
	}
	if tokens != 0 {
		t.Fatalf("expired lock-wait exchange minted %d credentials", tokens)
	}
}

func TestWorkerEnrollmentPendingCodeCannotResurrectRevokedCredential(t *testing.T) {
	reset(t)
	ctx := context.Background()
	account := newSupplierTestAccount(t, "enrollment-no-resurrection")
	device := newEnrollmentHTTPDevice(t)
	firstCode := issueEnrollmentCodeHTTP(t, account, device, nil)
	secondCode := issueEnrollmentCodeHTTP(t, account, device, nil)

	if code, _ := exchangeEnrollmentHTTP(t,
		enrollmentExchangeBody(t, firstCode, device, workerEnrollmentAudience, account.buyerID)); code != http.StatusUnauthorized {
		t.Fatalf("superseded pending code: want 401, got %d", code)
	}
	credential := mustExchangeEnrollmentHTTP(t,
		enrollmentExchangeBody(t, secondCode, device, workerEnrollmentAudience, account.buyerID))

	// An ordinary code cannot be issued while this fingerprint is active.
	code, out := req(t, http.MethodPost, "/v1/supplier/enrollment-codes", EnrollmentCodeIssueInput{
		Audience:           workerEnrollmentAudience,
		DeviceKeyAlgorithm: workerEnrollmentKeyAlgorithm,
		DevicePublicKey:    device.publicKey,
	}, jsonCT(), account.auth)
	if code != http.StatusConflict {
		t.Fatalf("active fingerprint reissue: want 409, got %d: %s", code, out)
	}

	// A pending same-device rotation is revoked together with its source.
	rotationCode := issueEnrollmentCodeHTTP(t, account, device, &credential.CredentialID)
	if err := itStore.RevokeWorkerCredentialForBuyer(ctx, account.buyerID, credential.CredentialID); err != nil {
		t.Fatal(err)
	}
	if code, _ := exchangeEnrollmentHTTP(t,
		enrollmentExchangeBody(t, rotationCode, device, workerEnrollmentAudience, account.buyerID)); code != http.StatusUnauthorized {
		t.Fatalf("pending rotation resurrected revoked credential: got %d", code)
	}
	var active int
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_tokens WHERE supplier_id=$1 AND revoked=false`,
		credential.SupplierID).Scan(&active); err != nil {
		t.Fatal(err)
	}
	if active != 0 {
		t.Fatalf("revocation left %d active credentials", active)
	}
}

func TestWorkerEnrollmentSourceDeletionCannotDowngradeRotation(t *testing.T) {
	reset(t)
	ctx := context.Background()
	account := newSupplierTestAccount(t, "enrollment-delete-source")
	firstDevice := newEnrollmentHTTPDevice(t)
	firstCode := issueEnrollmentCodeHTTP(t, account, firstDevice, nil)
	first := mustExchangeEnrollmentHTTP(t,
		enrollmentExchangeBody(t, firstCode, firstDevice, workerEnrollmentAudience, account.buyerID))
	secondDevice := newEnrollmentHTTPDevice(t)
	rotationCode := issueEnrollmentCodeHTTP(t, account, secondDevice, &first.CredentialID)

	if _, err := itPool.Exec(ctx, `DELETE FROM worker_tokens WHERE credential_id=$1`, first.CredentialID); err != nil {
		t.Fatalf("simulate credential retention deletion: %v", err)
	}
	var preserved *uuid.UUID
	if err := itPool.QueryRow(ctx, `SELECT rotate_from_credential_id FROM worker_enrollment_codes WHERE id=$1`,
		rotationCode.EnrollmentCodeID).Scan(&preserved); err != nil {
		t.Fatal(err)
	}
	if preserved == nil || *preserved != first.CredentialID {
		t.Fatalf("source deletion changed rotation semantics: got %v", preserved)
	}
	if code, _ := exchangeEnrollmentHTTP(t,
		enrollmentExchangeBody(t, rotationCode, secondDevice, workerEnrollmentAudience, account.buyerID)); code != http.StatusUnauthorized {
		t.Fatalf("source-deleted rotation downgraded to fresh enrollment: got %d", code)
	}
}

func TestWorkerEnrollmentRevokeRotationRaceLeavesNoActiveCredential(t *testing.T) {
	reset(t)
	ctx := context.Background()
	account := newSupplierTestAccount(t, "enrollment-revoke-race")
	for round := 0; round < 5; round++ {
		firstDevice := newEnrollmentHTTPDevice(t)
		firstCode := issueEnrollmentCodeHTTP(t, account, firstDevice, nil)
		first := mustExchangeEnrollmentHTTP(t,
			enrollmentExchangeBody(t, firstCode, firstDevice, workerEnrollmentAudience, account.buyerID))
		secondDevice := newEnrollmentHTTPDevice(t)
		rotationCode := issueEnrollmentCodeHTTP(t, account, secondDevice, &first.CredentialID)
		rotationBody := enrollmentExchangeBody(t, rotationCode, secondDevice, workerEnrollmentAudience, account.buyerID)

		start := make(chan struct{})
		exchangeDone := make(chan struct {
			result EnrollmentExchangeResult
			err    error
		}, 1)
		revokeDone := make(chan error, 1)
		go func() {
			<-start
			result, err := itStore.ExchangeWorkerEnrollmentCode(ctx, rotationBody)
			exchangeDone <- struct {
				result EnrollmentExchangeResult
				err    error
			}{result, err}
		}()
		go func() {
			<-start
			revokeDone <- itStore.RevokeWorkerCredentialForBuyer(ctx, account.buyerID, first.CredentialID)
		}()
		close(start)
		exchange := <-exchangeDone
		if exchange.err != nil && !errors.Is(exchange.err, errWorkerEnrollmentRejected) {
			t.Fatalf("round %d rotation race returned internal error: %v", round, exchange.err)
		}
		if err := <-revokeDone; err != nil {
			t.Fatalf("round %d revoke race failed: %v", round, err)
		}
		var active int
		if err := itPool.QueryRow(ctx, `SELECT count(*) FROM worker_tokens WHERE supplier_id=$1 AND revoked=false`,
			first.SupplierID).Scan(&active); err != nil {
			t.Fatal(err)
		}
		if active != 0 {
			t.Fatalf("round %d revoke/rotation race left %d active credentials", round, active)
		}
		if exchange.result.WorkerToken != "" {
			if _, err := itStore.LookupWorkerToken(ctx, exchange.result.WorkerToken); !errors.Is(err, errNotFound) {
				t.Fatalf("round %d raced rotation token still authenticates: %v", round, err)
			}
		}
	}
}

func TestWorkerEnrollmentSchemaRejectsPartialBindingAndSecretsAreNoStore(t *testing.T) {
	reset(t)
	ctx := context.Background()
	if _, err := itPool.Exec(ctx, `
		INSERT INTO worker_tokens
		  (token_hash,worker_id,supplier_id,device_public_key,device_fingerprint)
		VALUES ($1,$2,$3,$4,$5)`, hashKey(newSecret("cxw_")), demoWorkerUUID, demoSupplierUUID,
		make([]byte, 65), "partial-binding"); err == nil {
		t.Fatal("worker_tokens accepted a partial device binding")
	}

	account := newSupplierTestAccount(t, "enrollment-no-store")
	device := newEnrollmentHTTPDevice(t)
	issue := enrollmentRawRequest(http.MethodPost, "/v1/supplier/enrollment-codes", EnrollmentCodeIssueInput{
		Audience:           workerEnrollmentAudience,
		DeviceKeyAlgorithm: workerEnrollmentKeyAlgorithm,
		DevicePublicKey:    device.publicKey,
	}, jsonCT(), account.auth)
	if issue.err != nil || issue.status != http.StatusCreated {
		t.Fatalf("issue no-store request: status=%d err=%v body=%s", issue.status, issue.err, issue.body)
	}
	if issue.header.Get("Cache-Control") != "no-store" {
		t.Fatalf("secret code response is cacheable: %q", issue.header.Get("Cache-Control"))
	}
}
