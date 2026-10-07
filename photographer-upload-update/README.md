# Wedding photos: selfie-unlocked gallery

Guests enter the event code, take a selfie, and see every public photo plus the private photos they appear in.

```
guest phone ── selfie ──▶ API Gateway ──▶ Lambda ──▶ Rekognition SearchFacesByImage
                                             │            (returns photo IDs)
                                             ├──▶ DynamoDB (session: which private photos)
                                             └──▶ S3 pre-signed links (2 h)
your laptop ── upload_photos.py ──▶ S3 (thumb / web / original) + Rekognition IndexFaces
```

## What's here

| Path | Purpose |
|---|---|
| `template.yaml` | AWS SAM: private S3 bucket, sessions table, HTTP API with throttling, Lambda |
| `backend/app.py` | `POST /match` (selfie → session + gallery) and `GET /gallery` (returning guest) |
| `processor/app.py` | Runs automatically on each photographer upload: resize, index faces, publish |
| `scripts/upload_photos.py` | Admin alternative: resize, upload, and index faces from a local folder |
| `frontend/index.html` | Guest page: event code, selfie, gallery |
| `frontend/upload.html` | Photographer page: upload code, public/private drop zones |

## 1. Prerequisites

- AWS account, AWS CLI configured (`aws configure`), and the [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Python 3.10+
- Pick one region and use it everywhere. `eu-west-1` (Ireland) is a good default for guests in Nigeria.

## 2. Deploy the backend

```bash
sam build
sam deploy --guided --region eu-west-1
#   EventCode:     something short for the invitation cards, e.g. ADE2026
#   CollectionId:  wedding-faces (default)
#   MatchThreshold: 95 (default)
#   AllowedOrigin: * for now; set to your site URL once hosted, then redeploy
```

Note the `ApiUrl` and `BucketName` outputs.

## 3. Upload photos

```bash
cd scripts
pip install -r requirements.txt
python upload_photos.py --bucket <BucketName> --region eu-west-1 --visibility public  ../photos/shared
python upload_photos.py --bucket <BucketName> --region eu-west-1 --visibility private ../photos/everything-else
```

Re-running skips photos already uploaded, so you can add batches as the photographer delivers them. Guests' sessions store which private photos they matched, so after a new private batch, guests should tap "take another selfie" to pick up new matches.

## 4. Host the guest page

1. Edit `frontend/config.js`: paste the `ApiUrl`, the couple's names, and the date line.
2. Host the `frontend/` folder anywhere with HTTPS (the camera only works on HTTPS). Easiest: AWS Amplify Hosting → "Deploy without Git" → drag in the folder.
3. Set `AllowedOrigin` to that URL and run `sam deploy` again.
4. Share a link like `https://your-site/?code=ADE2026` on a QR code. The code pre-fills.

## Photographer uploads

Photographers get one link and nothing else:

```
https://<your-amplify-url>/upload.html#k=<UploadCode>
```

Add `&by=Studio%20Name` to pre-fill their name, which helps you trace who uploaded what. They put photos in two folders named **Photos for everyone** and **Private photos**, then drag both folders onto the page (or choose the folder containing them). Sorting is by folder name: any folder whose name contains "everyone" or "public" is public, and "private" is private. Photos outside either folder are uploaded as private, the safe default.

- The key sits after `#`, so browsers never send it to any server or in referrer headers. Treat the link like a password: anyone who has it can upload.
- **Turn a link off:** run `sam deploy --guided` with a new UploadCode, then send the new link to whoever still needs it.
- Generate a strong key in Git Bash: `openssl rand -hex 16`
- Edit the on-page guide by adding `uploadGuide` to `frontend/config.js`:
  ```js
  uploadGuide: {
    public:  ["Venue and decor", "Couple portraits"],
    private: ["Guest candids", "Anything you're unsure about"],
  },
  ```
- **Drive or Dropbox link instead:** download and unzip it, then drag the two folders onto the same upload page yourself.
- **Watch processing:** `sam logs -n ProcessorFunction --stack-name wedding-photos --region eu-west-1 --tail`
- **Re-trigger anything stuck in `incoming/`:**
  `aws s3 cp s3://<bucket>/incoming/ s3://<bucket>/incoming/ --recursive --metadata-directive REPLACE`

## 5. Test before the wedding

- Upload ~20 test photos of yourself and friends as private, a few as public.
- Try: correct match, a friend who isn't in any photo (should only see public), wrong event code, dim lighting, a group photo where you're in the back.
- If lookalikes or siblings match each other, raise `MatchThreshold` to 97–98.

## 6. After the wedding

Delete the face data once guests have downloaded their photos:

```bash
aws rekognition delete-collection --collection-id wedding-faces --region eu-west-1
```

Photos keep working for anyone already signed in until their session expires (30 days); new guests would then only see public photos. To retire everything, `sam delete`.

## Security notes

- Bucket is fully private; photos are served only through 2-hour pre-signed links issued after a match.
- The event code plus API throttling (10 req/s, burst 20) limits strangers probing the API.
- Selfies are compared and discarded; they are not written to S3 or DynamoDB.
- **Known gap in this version: no liveness check.** Someone holding up a photo of another guest could unlock that guest's private photos. Phase 2 adds Rekognition Face Liveness to close this.
- Anyone in a group photo can see that photo. That's the intended rule, but worth telling the couple.
