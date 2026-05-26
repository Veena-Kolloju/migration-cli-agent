# Pet Hotel
![header](https://github.com/user-attachments/assets/489879e1-76c7-44e1-894d-39548ad291f3)

This is a standard c-sharp project.

  - It has dotnet 6 as its version. 

  - Postgres is ready to go.
 
  - The Client App is in different Repo: https://github.com/PrimeAcademy/pethotel-dotnet-frontend

The client is available at `http://localhost:3000` and set up to proxy back to the dotnet API. 

The dotnet API is available at `http://localhost:5001/` 
and ALSO set up to proxy non-api requests to the webpack server. 


## Screenshots
### Pet Hotel User Interface 
![Pet Hotel Screenshot](pet-hotel.png)


### Passes NPM Tests 
![Passing Tests](tests.png)

### Features

Built the backend for the Pet Hotel. 
NPM start to run client
Built the HTTP API until all of the functionality of the Client App is working:

   - All 11 jest tests are passing 
   - Loading the page shows Pets and Pet Owners in the table.
   - User can add a new Pet Owner (and see error validations).
   - User can add new Pets, associated with pet owners (and see error validations).
   - User can check in a pet and see the timestamp on the table.
   - User can check out a pet.
   - User can delete pets.
   - User can delete pet owners.

### Plan
   1. Created models
   2. Added them to ApplicationContext
   3. Migrated database with `dotnet ef migrations` and `dotnet ef database update`
   4. Created controllers
   5. Tested controllers with PostMan and the jest tests
   6. Made sure the ReactApp is working appropriately



