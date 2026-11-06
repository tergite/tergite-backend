# Authentication and Authorization

Most of the heavy lifting for authentication is carried out by [MSS](https://github.com/tergite/tergite-frontend).
Have a look at the [MSS auth documentation](https://github.com/tergite/tergite-frontend/blob/main/docs/auth.md)
to understand the authentication flow.

## Authentication between backend and frontend

### How MSS authenticates BCC's

BCC should be able to send events like 'initialized', 'recalibrated', 'job_updated', along with fresh data like
device parameters, calibration data and job results.

- In order for only authentic backends to send data to MSS, we use BCC's `BCC_PRIVATE_KEY_FILE` (RSA) PEM file to sign
  websocket connections to MSS.
- MSS on the other end has the public key of each BCC, at its `[[backends]]` entry's `public_key_path`, and a list of
  all allowed BCC's in its config file.
- Each websocket connection to the `/devices/ws/{device-name}` must have a number of custom headers and a signature
    - `x-id` (contains name of device)
    - `x-request-id` (contains random secret that is always different for each request)
    - `x-signature` (contains the signed `<x-id>-<x-request-id>-<x-timestamp>`)
    - `x-timestamp` (contains the timestamp when the signature was made. the signature is valid for only a given time-to-live)
- MSS checks this signature using that BCC's public key.  
  It must have been generated with the private key of the device of the given `{device-name}` and it must not be expired.  
  Otherwise, the connection is rejected.

### How BCC authenticates MSS

MSS should be able to send data and request for other data from BCC. 

- In order for only authentic MSS to talk to BCC, MSS uses its private key (RSA) PEM file to sign a secret
- The secret is attached to the headers of the request. The request must have at least the following headers
    - `x-mss-user-id` (contains id of the user who from whom the original request to MSS was made, or none if no user was involved)
    - `x-mss-request-id` (contains random secret that is always different for each request)
    - `x-mss-signature` (contains the signed secret)
    - `x-mss-timestamp` (contains the timestamp when the signature was made. the signature is valid for only a given time-to-live)
    - `x-mss-is-admin` (contains the 'True' if the user, from whom the original request in MSS came)
- The signed secret is `<x-mss-user-id>|<x-mss-request-id>|<x-mss-timestamp>|<x-mss-is-admin, or empty>|<HTTP method>|<URL path>`,
  so the headers cannot be given admin or moved to another route
- BCC validates this signature using MSS's public key at `BCC_MSS_PUBLIC_KEY_PATH`. It rejects requests with invalid
  signatures, and those whose timestamp is in the future or older than `BCC_MSS_NONCE_TTL` seconds.
- BCC accepts each `x-mss-request-id` once: it claims the nonce in its SQL database, for `BCC_MSS_NONCE_TTL` seconds,
  before the route runs, so a request cannot be replayed while it is in flight, nor after BCC restarts.

## Job tokens

A user's SDK uploads a job to BCC directly, not through MSS, so it needs a credential of its own.

- MSS asks for one with `POST /token`, signed as above, naming the user and the job's id.
- BCC creates the user if it does not know them, and returns a JWT for that job alone: its `sub` is the user's id, its
  `job` the job's id, and it expires after `BCC_JWT_TTL` seconds. It is signed with `BCC_JWT_SECRET` and encrypted with
  MSS's public key, so that only MSS can read it.
- MSS decrypts the token and hands it to the user, whose SDK sends it as `Authorization: Bearer <token>` with the job
  file to `POST /jobs`. BCC refuses the upload if the file's `job_id` is not the token's job, or the job exists already.
- The same token cancels that job (`POST /jobs/{job_id}/cancel`) and downloads its log file
  (`GET /logfiles/{job_id}`) without MSS; the other job routes take MSS's signed headers.

## Who may call what

Every path may be prefixed with a device's name (`/loke/jobs`); without one it reaches the first device.
Interactive docs are at `/docs`.

| Path | Auth | Notes |
| --- | --- | --- |
| `GET /` | MSS | a greeting, for MSS to check its signature |
| `GET /health` | none | `{"status": "ok"}` while the API runs |
| `/static-properties`, `/dynamic-properties` | MSS | the device's properties and calibration |
| `POST /token` | MSS | a job token; creates the user if new |
| `/me` | MSS | `GET` the user's profile; `DELETE` it, cancelling their jobs and bookings |
| `/users`, `/users/{user_id}` | MSS admin | create, list, delete |
| `/bookings` | MSS | create, list, `/{booking_id}/cancel`, `/config` |
| `/jobs` | job token or MSS | `POST` uploads (token); `GET` lists (MSS), `GET`/`DELETE /{job_id}` (MSS); `/{job_id}/cancel` (either) |
| `/logfiles/{job_id}` | job token | the job's results, as HDF5 |
| `/recalibration` | MSS admin | `/init`, `/info`, `/cancel` |
| `/switch` | MSS admin, or MSS for `/status` | `/off`, `/on`, `/status` |

Admin-only routes use `get_verified_mss_admin_user_id`, which requires `x-mss-is-admin`. A job's or booking's
routes also check that the user owns it, or is an admin.
