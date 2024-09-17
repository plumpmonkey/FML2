from imports import *
from datasets import *
from flower_client import FlowerClient
from models import SparseAutoencoder

train_transform = transforms.Compose([
    transforms.Resize((256, 256)),  # Resize images to 256x256
    transforms.ToTensor(),  # Convert image to tensor
    transforms.Normalize((0.5,), (0.5,))  # Normalize for 1 channel
])

test_transform = transforms.Compose([
    transforms.Resize((256, 256)),  # Resize images to 256x256
    transforms.ToTensor(),  # Convert image to tensor
    transforms.Normalize((0.5,), (0.5,))  # Normalize for 1 channel
])

# Load dataset
train_transform = transforms.Compose([transforms.Resize((256, 256)), transforms.ToTensor(), transforms.Normalize((0.5,), (0.5,))])
test_transform = transforms.Compose([transforms.Resize((256, 256)), transforms.ToTensor(), transforms.Normalize((0.5,), (0.5,))])

NUM_CLIENTS = 10
trainloaders, testloader = load_datasets(NUM_CLIENTS, 'Dataset', train_transform, test_transform)

def client_fn(cid) -> FlowerClient:
    net = SparseAutoencoder().to(DEVICE)  # Assuming SparseAutoencoder is the model class
    trainloader = trainloaders[int(cid)]
    optimizer = torch.optim.Adam(net.parameters(), lr=0.001)  # Set your desired learning rate here
    scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=3, verbose=True)
    return FlowerClient(cid, net, trainloader, optimizer, scheduler, epochs_per_round=3)  # Adjust number of epochs per client per round


def train(net, trainloader, epochs: int, optimizer):
    """Train the autoencoder on the training set."""
    criterion = torch.nn.MSELoss()
    net.train()
    for epoch in range(epochs):
        total_loss = 0.0
        for batch in trainloader:
            images = batch.to(DEVICE)  # Assuming the DataLoader returns a tuple (images, labels)
            optimizer.zero_grad()
            outputs = net(images)
            loss = criterion(outputs, images)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        average_loss = total_loss / len(trainloader)
        print(f"Epoch {epoch+1}: train loss {average_loss:.4f}")
        
def test(net, testloader):
    """Evaluate the autoencoder on the test set."""
    criterion = torch.nn.MSELoss()
    total_loss = 0.0
    net.to(DEVICE)  # Move model to GPU if available
    net.eval()
    with torch.no_grad():
        for batch in testloader:
            images = batch[0].to(DEVICE)  # Move data to GPU if available
            outputs = net(images)
            loss = criterion(outputs, images)
            total_loss += loss.item()
    average_loss = total_loss / len(testloader)
    print(f"Test loss: {average_loss:.4f}")
    return average_loss

def get_parameters(net) -> List[np.ndarray]:
    # Return the parameters of the network as a list of NumPy arrays.
    return [val.cpu().numpy() for _, val in net.state_dict().items()]

def set_parameters(net, parameters: List[np.ndarray]):
    # Set the parameters of the network from a list of NumPy arrays.
    params_dict = zip(net.state_dict().keys(), parameters)  # Pair parameter names with given arrays.
    # Create an ordered dictionary of parameters, converting arrays to tensors.
    state_dict = OrderedDict({k: torch.Tensor(v) for k, v in params_dict})  
    net.load_state_dict(state_dict, strict=True)  # Load the parameters into the network.
