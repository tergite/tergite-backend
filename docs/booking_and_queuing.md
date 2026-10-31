# Booking and queueing

By default, jobs run on a first-come-first-serve basis, but it is also possible to 
book a time slot to accept only one user's jobs and waitlist other users' jobs.

## Possible operations

- [x] User creation via MSS
- [x] Booking creation
- [x] User login via MSS to get JWT token encrypted with MSS public key
- [x] Submit jobs with active booking; waitlisting all jobs of non-bookers, and queueing those of booker
- [x] Run waitlisted jobs when booking becomes idle
- [x] Creation of bookings overlapping with other bookings is not permitted
- [x] Creation of bookings beyond the maximum per day is not permitted.
- [x] User can delete their own booking as long as it has not yet started.
- [x] User can view all other user's bookings (paginated)
- [x] Admin user can cancel any user's booking
- [x] User can cancel a job that is yet to complete as long as it belongs to them.
- [x] Admin user can delete any user (automatically cancelling all their jobs and bookings)
- [x] User can delete their account, (automatically cancelling all their jobs and bookings)
- [x] User can view their own user profiles
- [x] Admin can view all users (paginated)
- [x] User can view their own jobs, both many and single

## Dependencies

- [SQLite](https://www.sqlite.org/), or another database SQLAlchemy supports, which holds the bookings,
  the jobs, the device parameters and switch, and MSS's nonces


## Operations (REST API)

- Start BCC as the [README](../README.md#quick-start) says; it serves on `BCC_PORT`, 8000 by default.
  The `x-mss-*` headers below stand for those MSS signs, as [auth.md](./auth.md) describes.

- Through, MSS, create users e.g.

```shell
curl -L 'http://127.0.0.1:8000/users' \
-H 'Content-Type: application/json' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420' \
-H 'x-mss-is-admin: True' \
--data-raw '{
    "id": "0f29a380-3a02-4c6f-86f9-340574d747aa",
    "name": "John Doe",
    "email": "johndoe@example.com"
}'
```

Responding with: 

```json
{
    "id": "0f29a380-3a02-4c6f-86f9-340574d747aa",
    "name": "John Doe",
    "email": "johndoe@example.com"
}
```

- Through MSS, create a booking for one user e.g..

```shell
curl -L 'http://127.0.0.1:8000/bookings' \
-H 'Content-Type: application/json' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420' \
-d '{
    "start_utc": "2025-07-18T17:48:58.619Z",
    "end_utc": "2025-07-18T17:52:58.619Z"
}'
```

Responding with something like:

```json
{
  "id": "68530559-b54d-4ffe-9f3b-d17bbf141c43",
  "user_id": "105f22e1-6b2a-4b0e-a8a1-0c35309cb420",
  "start_utc": "2025-07-18T17:48:58.619Z",
  "end_utc": "2025-07-18T17:52:58.619Z"
}
```

- Through MSS, get token for submitting a job later e.g.

```shell
curl -L 'http://127.0.0.1:8000/token' \
-H 'Content-Type: application/json' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420' \
--data-raw '{
    "user_id": "105f22e1-6b2a-4b0e-a8a1-0c35309cb420",
    "job_id": "17be2242-ebd1-46c4-b249-8bbe018d321c"
}'
```

Responding with something like the following, whose `access_token` is the job's JWT encrypted with MSS's
public key, which MSS decrypts before handing it to the user:

```json
{
  "access_token": "kVb3Qx0iGm4...base64 of the RSA-OAEP ciphertext...Wf9sYw==",
  "token_type": "bearer"
}
```

- Directly submit jobs using the decrypted `access_token` e.g.

```shell
curl -L 'http://127.0.0.1:8000/jobs' \
-H 'Content-Type: application/json' \
-H 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiYWRtaW4iOnRydWUsImlhdCI6MTUxNjIzOTAyMn0.KMUFsIDTnFmyG3nMiGM6H9FNFUROf3wh7SmqJp-QV30' \
-F 'upload_file=@"/Users/johndoe/jobs/job_17be2242-ebd1-46c4-b249-8bbe018d321c.json"'
```

Responding like:

```json
{
  "id": "17be2242-ebd1-46c4-b249-8bbe018d321c",
  "user_id": "105f22e1-6b2a-4b0e-a8a1-0c35309cb420",
  "status": "pending"
  // etc.
}
```

- Through MSS, you can also view a given job you submitted.

```shell
curl -L 'http://127.0.0.1:8000/jobs/17be2242-ebd1-46c4-b249-8bbe018d321c' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420'
```

- Through MSS, you can cancel a job you submitted (or if you are admin)

```shell
curl -L 'http://127.0.0.1:8000/jobs/17be2242-ebd1-46c4-b249-8bbe018d321c/cancel' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420' \
--data-raw '{
    "reason": "I just felt like it!"
}'
```

- Through MSS, you can also delete a job you submitted (or if you are admin)

```shell
curl -L -X DELETE 'http://127.0.0.1:8000/jobs/17be2242-ebd1-46c4-b249-8bbe018d321c' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420'
```


- Through MSS, you can also view a list of jobs.

```shell
curl -L 'http://127.0.0.1:8000/jobs?skip=1&limit=10' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420'
```

- Through MSS, you can also view a list of bookings.

```shell
curl -L 'http://127.0.0.1:8000/bookings?skip=1&limit=10' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420'
```

- Through MSS, you can cancel your own booking given that you know the booking ID.

```shell
curl -L -X POST 'http://127.0.0.1:8000/bookings/68530559-b54d-4ffe-9f3b-d17bbf141c43/cancel' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420'
```


## Basic flow

### Creating a booking

- User sends `Booking` data `(start_utc, end_utc)` timestamp pair.
- If `start_utc` falls in the span of another booking or `end_utc` falls in the span of another booking,
  an error is raised
- Otherwise, the booking is saved to the list of bookings with its unique ID.

_Note: The user must specify the backend they are booking when submitting request through MSS_.

### Deleting a booking

- User requests to delete a Booking of the given ID
- It is deleted from the list of bookings, unless it has started.

### Running the jobs

- A job is a row of the jobs table, with its stage and its estimated duration, which
  preprocessing, the compilation of its schedules, sets.
- Each device's heavy worker runs, of the jobs the table holds, the execution the bookings let run
  next, else the oldest preprocessing, else a recalibration that is due. A light worker
  postprocesses the oldest executed job of every device.
- A worker claims a job in one transaction, so no two workers run the same stage of a job.
- A worker with nothing to claim waits until the API or the heavy worker wakes it, with a
  datagram to the loopback port it names in `<storage root>/<device>/wakeups/`. A heavy worker
  with a job waiting to execute also wakes itself at the next moment the rules below could pick
  a different job: the active booking's end, its booker turning idle, or the next booking's
  start. No worker waits more than `BCC_MAX_WORKER_WAIT_TIME`, 5 seconds by default.
- [DESIGN.md §6](../DESIGN.md#6-queueing-the-dispatcher) describes this in full, with diagrams.

### When there is no active booking:

- The oldest preprocessed job that ends before the next booking starts runs next.
  A longer job waits, and shorter ones run before it, until it fits.
- A job that has waited longer than `MAX_EXECUTION_QUEUE_TIME`, a day by default, is failed, and its
  user told, rather than left to wait.

### When a booking is active:

- The booker's oldest preprocessed job that ends before the booking does runs next.
- A booker's job that finishes preprocessing during their booking, and is longer than what is
  left of it, fails, and alerts the user that the job is too long for the slot.  
  _(The reason here is to allow the user to decide if they want to elongate 
  their time slot or just put the job on the normal queue.)_
- A booker's job with `force_normal_queue=True`, and any other user's job, waits.
- Once the booker has had no job executing for `MAX_IDLE_TIME`, the oldest job of anyone's
  that ends before the next booking starts runs, one at a time, until the booker has a job
  ready to execute that fits what is left of their booking.
- A job under way when the booking ends runs to completion; then the rules for no active
  booking apply.

### Recalibration

- A recalibration is a row with a due time. A heavy worker runs it when its device has no job to
  run, having first scheduled the next one interval later.

## Constraints

- A user can book only `MAX_SLOTS_PER_DAY` time slots per day. 
- A time slot can not be shorter than `MIN_TIME_SLOT_LENGTH` seconds, nor longer than `MAX_TIME_SLOT_LENGTH`.
- Each booking is associated with one backend and one user.
- Each booking has a unique ID for easy querying and deletion.

## Opportunities

- Since it is possible to precalculate the length of a job basing on the compiled schedules.
  - We can estimate the time when a submitted job will be run basing on 
    its position in the queue it is in, and the jobs or time slots that   
    must complete before it runs.  
    This can be vital information for users.
- It might be possible to cancel a job using [InstrumentController.stop()](https://quantify-os.org/docs/quantify-scheduler/v0.24.0/autoapi/quantify_scheduler/instrument_coordinator/index.html#quantify_scheduler.instrument_coordinator.InstrumentCoordinator.stop)
  But that probably means we always need to call `InstrumentController.start()` always.  
  - This can be used in our software circuit breakers that stop a job in case something is seriously going wrong.

## Inspiration

Inspiration was obtained from:

- [IBM's Session mode](https://docs.quantum.ibm.com/guides/execution-modes#session-mode)
