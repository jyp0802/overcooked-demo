sudo -v
if [[ $1 = prod* ]];
then
    echo "production"
    export BUILD_ENV=production

    # Completely re-build all images from scatch without using build cache
    sudo docker-compose build --no-cache
    sudo docker-compose up --force-recreate -d
else
    echo "development"
    export BUILD_ENV=development
    # Uncomment the following line if there has been an updated to overcooked-ai code
    # sudo docker-compose build --no-cache

    # Force re-build of all images but allow use of build cache if possible
    sudo docker-compose up --build
fi