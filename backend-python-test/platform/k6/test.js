import http from 'k6/http';
import { check, sleep } from 'k6';

export let options = {
  stages: [
    { duration: '10s', target: 50 },
    { duration: '20s', target: 150 },
    { duration: '10s', target: 200 },
  ],
};

const BASE_URL = __ENV.TARGET_URL || 'http://localhost:5000';

export default function () {
  let payload = JSON.stringify({
    to: "user@example.com",
    message: "Test notification",
    type: "email"
  });
  
  let params = {
    headers: { 'Content-Type': 'application/json' },
  };

  let createRes = http.post(`${BASE_URL}/v1/requests`, payload, params);
  
  check(createRes, {
    'create status is 201 or 200': (r) => r.status === 201 || r.status === 200,
    'id is present in response': (r) => {
      try { return JSON.parse(r.body).id !== undefined; }
      catch (e) { return false; }
    },
  });

  if (createRes.status === 201 || createRes.status === 200) {
    let id;
    try {
      id = JSON.parse(createRes.body).id;
    } catch (e) {
      return;
    }

    let processRes = http.post(`${BASE_URL}/v1/requests/${id}/process`);
    check(processRes, {
      'process status is 202 or 200': (r) => [200, 202].includes(r.status),
    });

    sleep(0.5); 
    let statusRes = http.get(`${BASE_URL}/v1/requests/${id}`);
    check(statusRes, {
      'status is retrieved': (r) => r.status === 200,
      'status body is valid json': (r) => {
        try { JSON.parse(r.body); return true; }
        catch (e) { return false; }
      },
      'status value is valid': (r) => {
        try { return ['queued', 'processing', 'sent', 'failed'].includes(JSON.parse(r.body).status); }
        catch (e) { return false; }
      },
    });
  }

  sleep(1);
}
