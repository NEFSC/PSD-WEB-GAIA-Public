# GAIA's WHALE TCPED
---

### Description
Geospatial Artificial Intelligence for Animals 

The Geospatial Artificial Intelligence for Animals (GAIA) program brings together a broad coalition of partners working to advance satellite-based marine wildlife monitoring through scalable cloud computing, remote sensing, and emerging artificial intelligence workflows. Collaborators include government agencies such as the National Oceanic and Atmospheric Administration (NOAA), U.S. Naval Research Laboratory, the Bureau of Ocean Energy Management (BOEM), and the U.S. Geological Survey; research institutions including the British Antarctic Survey; academic partners such as the University of Edinburgh and University of Minnesota; and private sector collaborators including Microsoft AI for Good Research Lab and Vantor.

GAIA’s cloud application supports the ingestion, preprocessing, organization, and expert annotation of very high-resolution satellite imagery to facilitate marine wildlife detection and generate high-quality labeled training data for future AI-assisted workflows.

GAIA's WHale Active Learning Environment (WHALE) Tasking, Collection, Processing, Annotation, and Dissimination (TCPED) Application is a port of the original [WHALE](https://github.com/microsoft/whale), created by Microsoft AI for Good in collaboration with NOAA, to GeoDjango and expanded to handle satellite imagery collection and dissimination needs of the GAIA team.

### Contents
- gaia - GeoDjango project directory 
- animal - GeoDjango application directory
     - management - Files for managing the application (e.g., creating logs)
     - migrations - Migration files
     - templates - HTML templates, or webpages, for the application.

### Local Development Environment Debug
- Grab local spatialite database file, adjust environment
- Set environment to debug and start:
     ```
     settings.py: DEBUG = True

     docker compose -f docker-compose-no-nginx-debug.yml up
     ```

     This will bring up all of the services.

- Run:Start debugging in vs code and set breakpoints

     Try it in browser and see if it hits a breakpoint.

- Once can exec into the running containers and create tasks with Docker
Compose:
     ```
     docker compose exec web  /bin/bash
     ```
     Then:
     ```
     python manage.py shell

     from gaia.celery import app  # or wherever your Celery app is defined
     from animal.tasks import your_task_name  # replace with your actual task
     ```
- Call the task asynchronously
     ```
     your_task_name.delay(*args)
     ```
- Or synchronously (for testing)
     ```
     result = your_task_name.apply(args=(...), kwargs={...})
     print(result.get())
     ```

     Then watch task in flower by going to /monitor

### Disclosure Statement
This repository is a scientific product and is not official communication of the National Oceanic and Atmospheric Administration, or the United States Department of Commerce. All NOAA GitHub project code is provided on an ‘as is’ basis and the user assumes responsibility for its use. Any claims against the Department of Commerce or Department of Commerce bureaus stemming from the use of this GitHub project will be governed by all applicable Federal law. Any reference to specific commercial products, processes, or services by service mark, trademark, manufacturer, or otherwise, does not constitute or imply their endorsement, recommendation or favoring by the Department of Commerce. The Department of Commerce seal and logo, or the seal and logo of a DOC bureau, shall not be used in any manner to imply endorsement of any commercial product or activity by DOC or the United States Government.
